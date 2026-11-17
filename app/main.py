"""Alarm escalation API.

Run with: ``uvicorn app.main:app``
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, status

from . import __version__
from .config import Settings
from .db import Database, parse_ts, utc_now
from .gateway_client import GatewayClient
from .models import (
    AlarmCreate,
    AlarmView,
    CabinetView,
    ConfirmRequest,
    HealthView,
    NotificationList,
    NotificationView,
    SampleCreate,
    SampleView,
    ThermalEventList,
    ThermalEventView,
)
from .service import AlarmService, ConflictError, NotFoundError
from .thermal import (
    CabinetNotFoundError,
    RejectedSampleError,
    SampleConflictError,
    ThermalService,
)
from .worker import Dispatcher

log = logging.getLogger("alarm")


def _notification_list_view(alarm_view: dict) -> list[dict]:
    return alarm_view["notifications"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db = Database(settings.database_path)
    service = AlarmService(db)
    thermal = ThermalService(db, settings, service)
    gateway = GatewayClient(
        settings.gateway_url,
        post_timeout=settings.post_timeout_seconds,
        lookup_timeout=settings.lookup_timeout_seconds,
    )
    dispatcher = Dispatcher(
        db,
        service,
        gateway,
        interval=settings.worker_interval_seconds,
        lease_seconds=settings.delivery_lease_seconds,
        max_backoff_seconds=settings.max_backoff_seconds,
        max_concurrency=settings.delivery_concurrency,
        not_found_misses_before_repost=settings.not_found_misses_before_repost,
        thermal_service=thermal,
        thermal_interval=settings.thermal_tick_interval_seconds,
    )
    app.state.db = db
    app.state.service = service
    app.state.thermal = thermal
    app.state.gateway = gateway
    app.state.dispatcher = dispatcher
    if settings.recover_inflight_on_startup:
        recovered = dispatcher.recover_interrupted()
        if recovered:
            log.info("recovered %d interrupted notification(s) after restart", recovered)
    await dispatcher.start()
    try:
        yield
    finally:
        await dispatcher.stop()
        await gateway.aclose()
        db.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="机房告警升级 API",
        version=__version__,
        description=(
            "供电柜过热告警的升级通知 API。\n\n"
            "**流程**：柜温采样（`/cabinets/{id}/samples`）→ 连续三笔 ≥70°C 且相邻间隔 "
            "≤60 秒触发过热事件（120 秒窗口内允许乱序，窗口未关仅为待定）→ "
            "确认事件创建一条告警并通知主值班员 → 网关确已受理后开始计入确认期限 → "
            "逾期未确认仅投递一次备班通知；确认后不再创建新的备班投递。\n\n"
            "**关键语义**\n"
            "- 同柜同采样时刻同温度重送幂等；异温度拒绝（409）；超出 120 秒窗口的采样拒绝（422）。\n"
            "- 过热事件在判定窗口关闭后结论不可被旧样本改写；迟到的低温读数只能打断尚未关窗的连续高温。\n"
            "- 同一告警号 + 相同内容的重试幂等复用原告警；内容不一致返回 409。\n"
            "- 主通知只有拿到网关的受理确认才开始计时；回执不明标记为 "
            "`receipt_unconfirmed`，查明前绝不触发升级。\n"
            "- 通知号是网关幂等键；API 重启后按数据库中的待办继续投递/查询。\n"
            "- 确认与到期处理并发时，确认优先且不新建备班投递；已在途通知通过网关查询后如实显示。"
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings or Settings()

    @app.get("/health", response_model=HealthView, tags=["运维"], summary="健康检查")
    def health(request: Request) -> HealthView:
        db: Database = request.app.state.db
        try:
            db.query_one("SELECT 1 AS ok")
            db_state = "ok"
        except Exception as exc:  # noqa: BLE001 - health must always answer
            db_state = f"error: {exc}"
        return HealthView(
            status="ok" if db_state == "ok" else "degraded",
            database=db_state,
            gateway_url=request.app.state.settings.gateway_url,
        )

    @app.post(
        "/alarms",
        response_model=AlarmView,
        status_code=status.HTTP_201_CREATED,
        tags=["告警"],
        summary="提交告警并通知主值班员",
        responses={
            200: {"description": "相同告警号与内容的幂等重试，返回已存在的告警"},
            409: {"description": "告警号已存在但内容不同"},
        },
    )
    async def submit_alarm(payload: AlarmCreate, request: Request, response: Response) -> dict:
        service: AlarmService = request.app.state.service
        try:
            view, deduplicated = service.submit_alarm(payload)
        except ConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        view["deduplicated"] = deduplicated
        if deduplicated:
            response.status_code = status.HTTP_200_OK
        return view

    async def _reconcile(request: Request, alarm_no: str | None = None) -> None:
        """Resolve ambiguous receipts before rendering a status.

        This performs gateway lookups (bounded by the lookup timeout) but no
        posts. It only ever touches unresolved notifications, and gateway calls
        happen outside database transactions, so a slow lookup cannot block a
        confirmation for another alarm.
        """
        settings: Settings = request.app.state.settings
        if not settings.reconcile_on_read:
            return
        dispatcher: Dispatcher = request.app.state.dispatcher
        db: Database = request.app.state.db
        try:
            if alarm_no is None:
                await dispatcher.reconcile_unknown(limit=10)
            else:
                rows = db.query(
                    "SELECT 1 FROM notifications WHERE alarm_no = ? AND state = 'unknown' LIMIT 1",
                    (alarm_no,),
                )
                if rows:
                    await dispatcher.reconcile_unknown(limit=10, alarm_no=alarm_no)
        except Exception:  # status rendering must still work if gateway is down
            log.warning("reconciliation during read failed", exc_info=True)

    @app.get(
        "/alarms/{alarm_no}",
        response_model=AlarmView,
        tags=["告警"],
        summary="查询告警、通知与确认状态",
        description=(
            "返回主/备通知的网关受理状态与已确认结果。若主通知回执不明，会先向网关"
            "查询受理结果；查明前 `status` 为 `primary_unconfirmed` 且计时未开始。"
        ),
    )
    async def get_alarm(alarm_no: str, request: Request) -> dict:
        service: AlarmService = request.app.state.service
        await _reconcile(request, alarm_no)
        try:
            return service.snapshot(alarm_no, now_real=parse_ts(utc_now()))
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"alarm {alarm_no!r} not found") from exc

    @app.post(
        "/alarms/{alarm_no}/confirm",
        response_model=AlarmView,
        tags=["告警"],
        summary="确认告警",
        description=(
            "确认后不再创建新的备班投递。若到期处理已生成备班通知但尚未发送，会在同一"
            "事务内取消；若备班通知已在途，则不会撤回，并在查询结果中如实显示其最终受理情况。"
        ),
        responses={404: {"description": "告警不存在"}},
    )
    async def confirm_alarm(alarm_no: str, payload: ConfirmRequest, request: Request) -> dict:
        service: AlarmService = request.app.state.service
        try:
            view = service.confirm(alarm_no, payload.confirmed_by, payload.note)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"alarm {alarm_no!r} not found") from exc
        # Deliberately no gateway reconciliation here: confirmation must stay
        # fast and must not wait on a slow notification gateway.
        return view

    @app.get(
        "/alarms/{alarm_no}/notifications",
        response_model=NotificationList,
        tags=["告警"],
        summary="查询该告警的通知投递明细",
    )
    async def list_notifications(alarm_no: str, request: Request) -> NotificationList:
        service: AlarmService = request.app.state.service
        await _reconcile(request, alarm_no)
        try:
            view = service.snapshot(alarm_no, now_real=parse_ts(utc_now()))
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"alarm {alarm_no!r} not found") from exc
        items = [NotificationView(**item) for item in _notification_list_view(view)]
        return NotificationList(items=items, total=len(items))

    @app.get(
        "/notifications/{notification_no}",
        response_model=NotificationView,
        tags=["通知"],
        summary="按通知号查询网关受理结果",
        description="只返回本 API 已持久化的通知；回执不明时会先向网关查询再展示。",
    )
    async def get_notification(notification_no: str, request: Request) -> NotificationView:
        db: Database = request.app.state.db
        row = db.query_one(
            "SELECT alarm_no FROM notifications WHERE notification_no = ?", (notification_no,)
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"notification {notification_no!r} not found")
        await _reconcile(request, row["alarm_no"])
        service: AlarmService = request.app.state.service
        view = service.snapshot(row["alarm_no"], now_real=parse_ts(utc_now()))
        for item in view["notifications"]:
            if item["notification_no"] == notification_no:
                return NotificationView(**item)
        raise HTTPException(status_code=404, detail=f"notification {notification_no!r} not found")

    @app.get(
        "/alarms",
        tags=["运维"],
        summary="列出告警（按创建时间倒序）",
    )
    async def list_alarms(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        status_filter: str | None = Query(default=None, alias="status"),
    ) -> dict:
        db: Database = request.app.state.db
        rows = db.query(
            "SELECT alarm_no FROM alarms ORDER BY created_real DESC LIMIT ?", (limit,)
        )
        service: AlarmService = request.app.state.service
        now_real = parse_ts(utc_now())
        views = [service.snapshot(r["alarm_no"], now_real=now_real) for r in rows]
        if status_filter:
            views = [v for v in views if v["status"] == status_filter]
        return {"items": views, "total": len(views)}

    # ------------------------------------------------------------- thermal API

    thermal_tags = ["柜温过热判定"]

    @app.post(
        "/cabinets/{cabinet_id}/samples",
        response_model=SampleView,
        status_code=status.HTTP_202_ACCEPTED,
        tags=thermal_tags,
        summary="上传柜温采样",
        description=(
            "采集端按柜号上传采样时间（时区感知 ISO-8601）与摄氏温度。\n\n"
            "- 只接收距接收时刻不超过 120 秒（`THERMAL_SAMPLE_WINDOW_SECONDS`）的采样，"
            "允许乱序；未来时间与过旧采样返回 422。\n"
            "- 同柜同采样时刻同温度重送幂等（200，不重复计数）；同柜同时刻异温度返回 409。\n"
            "- 上传后立即重算该柜的开放判定窗口；窗口未关时只展示待定，结论由后台关窗后落定。"
        ),
        responses={
            200: {"description": "完全相同的采样重送，复用已存储记录"},
            409: {"description": "同柜同采样时刻但温度不同"},
            422: {"description": "采样超出 120 秒接收窗口或时间格式非法"},
        },
    )
    async def submit_sample(
        cabinet_id: str,
        payload: SampleCreate,
        request: Request,
        response: Response,
        x_test_now_epoch: str | None = Header(default=None, include_in_schema=False),
    ) -> dict:
        cabinet_id = cabinet_id.strip()
        if not cabinet_id or len(cabinet_id) > 40:
            raise HTTPException(status_code=422, detail="cabinet_id 长度需在 1~40 之间")
        thermal: ThermalService = request.app.state.thermal
        if payload.sampled_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="sampled_at 必须带时区（UTC）")
        sampled_real = payload.sampled_at.timestamp()
        now_real: float | None = None
        if x_test_now_epoch is not None:
            if not request.app.state.settings.allow_test_clock:
                raise HTTPException(
                    status_code=400,
                    detail="X-Test-Now-Epoch 仅在 ALLOW_TEST_CLOCK=true 的测试环境可用",
                )
            try:
                now_real = float(x_test_now_epoch)
            except ValueError:
                raise HTTPException(
                    status_code=422, detail="X-Test-Now-Epoch 必须是 epoch 秒数值"
                ) from None
        try:
            row = thermal.ingest_sample(
                cabinet_id, sampled_real, payload.temperature_c, now_real=now_real
            )
        except RejectedSampleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SampleConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if row["deduplicated"]:
            response.status_code = status.HTTP_200_OK
        return {
            "cabinet_id": cabinet_id,
            "sampled_at": row["sampled_at"],
            "temperature_c": row["temperature_c"],
            "received_at": row["received_at"],
            "deduplicated": row["deduplicated"],
        }

    @app.get(
        "/cabinets",
        tags=thermal_tags,
        summary="列出有采样的柜号及其过热判定状态",
    )
    async def list_cabinets(request: Request) -> dict:
        thermal: ThermalService = request.app.state.thermal
        items = thermal.list_cabinets()
        return {"items": items, "total": len(items)}

    @app.get(
        "/cabinets/{cabinet_id}",
        response_model=CabinetView,
        tags=thermal_tags,
        summary="查询单柜状态（当前连续高温、待定事件、布防状态）",
        responses={404: {"description": "该柜号没有任何采样"}},
    )
    async def get_cabinet(cabinet_id: str, request: Request) -> dict:
        thermal: ThermalService = request.app.state.thermal
        try:
            return thermal.cabinet_snapshot(cabinet_id)
        except CabinetNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"cabinet {cabinet_id!r} not found") from exc

    @app.get(
        "/thermal-events",
        response_model=ThermalEventList,
        tags=thermal_tags,
        summary="查询过热事件（可按柜号/状态过滤）",
        description=(
            "返回每个事件判定所用的读数 `trigger_readings` 及关联告警 `alarm`。"
            "状态：pending（窗口未关，仅为待定）/ confirmed（已确认并建告警）/ "
            "interrupted（窗口关闭前被迟到低温等打断，未成立）。"
        ),
    )
    async def list_thermal_events(
        request: Request,
        cabinet_id: str | None = Query(default=None),
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        thermal: ThermalService = request.app.state.thermal
        items = thermal.list_events(
            cabinet_id=cabinet_id, status_filter=status_filter, limit=limit
        )
        return {"items": items, "total": len(items)}

    @app.get(
        "/thermal-events/{event_no}",
        response_model=ThermalEventView,
        tags=thermal_tags,
        summary="查询单个过热事件（含判定读数与关联告警）",
        responses={404: {"description": "事件不存在"}},
    )
    async def get_thermal_event(event_no: str, request: Request) -> dict:
        thermal: ThermalService = request.app.state.thermal
        try:
            return thermal.get_event(event_no)
        except CabinetNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"thermal event {event_no!r} not found") from exc

    return app


app = create_app()
