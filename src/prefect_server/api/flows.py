import datetime
import uuid
from typing import Any, Dict, List

import pendulum
from packaging import version as module_version
from pydantic import BaseModel, Field, validator

from prefect.serialization.schedule import ScheduleSchema
from prefect.utilities.graphql import with_args
from prefect import api
from prefect_server import config
from prefect_server.database import models
from prefect_server.utilities import logging
from prefect.utilities.plugins import register_api

logger = logging.get_logger("api.flows")
schedule_schema = ScheduleSchema()

# -----------------------------------------------------
# Schema for deserializing flows
# -----------------------------------------------------


class Model(BaseModel):
    class Config:
        # allow extra fields in case schema changes
        extra = "allow"


class ClockSchema(Model):
    parameter_defaults: Dict = Field(default_factory=dict)


class ScheduleSchema(Model):
    clocks: List[ClockSchema] = Field(default_factory=list)


class TaskSchema(Model):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = None
    slug: str
    type: str = None
    auto_generated: bool = False
    tags: List[str] = Field(default_factory=list)
    max_retries: int = 0
    retry_delay: datetime.timedelta = None
    cache_key: str = None
    trigger: str = None
    mapped: bool = False
    is_reference_task: bool = False
    is_root_task: bool = False
    is_terminal_task: bool = False

    @validator("trigger", pre=True)
    def _validate_trigger(cls, v):
        # core sends the trigger as an abbreviated dictionary
        if isinstance(v, dict):
            return v.get("fn")
        return v


class EdgeSchema(Model):
    upstream_task: str
    downstream_task: str
    mapped: bool = False
    key: str = None

    @validator("upstream_task", pre=True)
    def _validate_upstream_task(cls, v):
        # core sends the task as an abbreviated dictionary
        if isinstance(v, dict):
            return v.get("slug")
        return v

    @validator("downstream_task", pre=True)
    def _validate_downstream_task(cls, v):
        # core sends the task as an abbreviated dictionary
        if isinstance(v, dict):
            return v.get("slug")
        return v


class ParameterSchema(Model):
    name: str = None
    slug: str
    required: bool = False
    default: Any = None


class FlowSchema(Model):
    name: str = None
    tasks: List[TaskSchema] = Field(default_factory=list)
    edges: List[EdgeSchema] = Field(default_factory=list)
    parameters: List[ParameterSchema] = Field(default_factory=list)
    environment: Dict[str, Any] = None
    storage: Dict[str, Any] = None
    schedule: ScheduleSchema = None
    reference_tasks: List[str] = Field(default_factory=list)

    @validator("reference_tasks", pre=True)
    def _validate_reference_tasks(cls, v):
        reference_tasks = []
        for t in v:
            # core sends the task as an abbreviated dictionary
            if isinstance(t, dict):
                t = t.get("slug")
            reference_tasks.append(t)
        return reference_tasks


@register_api("flows._update_flow_setting")
async def _update_flow_setting(flow_id: str, key: str, value: any) -> models.FlowGroup:
    """
    Updates a single setting for a flow

    Args:
        - flow_id (str): the flow id
        - key (str): the flow setting key
        - value (str): the desired value for the given key

    Returns:
        - FlowGroup: the updated FlowGroup

    Raises:
        - ValueError: if flow ID is not provided or invalid
    """
    if flow_id is None:
        raise ValueError("Invalid flow ID")

    # retrieve current settings so that we only update provided keys
    flow = await models.Flow.where(id=flow_id).first(
        selection_set={"version_group_id": True, "flow_group": {"id", "settings"}}
    )

    # if we don't have permission to view the flow, we shouldn't be able to update it
    if not flow:
        raise ValueError("Invalid flow ID")

    flow.flow_group.settings[key] = value  # type: ignore

    # update with new settings
    result = await models.FlowGroup.where(id=flow.flow_group.id).update(
        set={"settings": flow.flow_group.settings},
        selection_set={"returning": {"id", "settings"}, "affected_rows": True},
    )  # type: ignore

    if not result.affected_rows:
        raise ValueError("Settings update failed")

    return models.FlowGroup(**result.returning[0])


@register_api("flows.create_flow")
async def create_flow(
    serialized_flow: dict,
    project_id: str,
    version_group_id: str = None,
    set_schedule_active: bool = True,
    description: str = None,
) -> str:
    """
    Add a flow to the database.

    Args:
        - project_id (str): A project id
        - serialized_flow (dict): A dictionary of information used to represent a flow
        - version_group_id (str): A version group to add the Flow to
        - set_schedule_active (bool): Whether to set the flow's schedule to active
        - description (str): a description of the flow being created

    Returns:
        str: The id of the new flow

    Raises:
        - ValueError: if the flow's version of Prefect Core falls below the cutoff

    """
    flow = FlowSchema(**serialized_flow)

    # core versions before 0.6.1 were used only for internal purposes-- this is our cutoff
    core_version = flow.environment.get("__version__", None)
    if core_version and module_version.parse(core_version) < module_version.parse(
        config.core_version_cutoff
    ):
        raise ValueError(
            "Prefect Serve requires new flows to be built with Prefect "
            f"{config.core_version_cutoff}+, but this flow was built with "
            f"Prefect {core_version}."
        )

    # load project
    project = await models.Project.where(id=project_id).first({"tenant_id"})
    if not project:
        raise ValueError("Invalid project.")
    tenant_id = project.tenant_id  # type: ignore

    # check required parameters - can't load a flow that has required params and a shcedule
    # NOTE: if we allow schedules to be set via UI in the future, we might skip or
    # refactor this check
    required_parameters = [p for p in flow.parameters if p.required]
    if flow.schedule is not None and required_parameters:
        required_names = {p.name for p in required_parameters}
        if not all(
            [required_names <= set(c.parameter_defaults) for c in flow.schedule.clocks]
        ):
            raise ValueError("Can not schedule a flow that has required parameters.")

    # set up task detail info
    task_lookup = {t.slug: t for t in flow.tasks}
    tasks_with_upstreams = {e.downstream_task for e in flow.edges}
    tasks_with_downstreams = {e.upstream_task for e in flow.edges}
    reference_tasks = set(flow.reference_tasks) or {
        t.slug for t in flow.tasks if t.slug not in tasks_with_downstreams
    }

    for t in flow.tasks:
        t.mapped = any(e.mapped for e in flow.edges if e.downstream_task == t.slug)
        t.is_reference_task = t.slug in reference_tasks
        t.is_root_task = t.slug not in tasks_with_upstreams
        t.is_terminal_task = t.slug not in tasks_with_downstreams

    # set up versioning
    version_group_id = version_group_id or str(uuid.uuid4())
    version_where = {
        "version_group_id": {"_eq": version_group_id},
        "tenant_id": {"_eq": tenant_id},
    }
    # set up a flow group if it's not already in the system
    flow_group = await models.FlowGroup.where(
        {
            "_and": [
                {"tenant_id": {"_eq": tenant_id}},
                {"name": {"_eq": version_group_id}},
            ]
        }
    ).first()
    if flow_group is None:
        flow_group_id = await models.FlowGroup(
            tenant_id=tenant_id,
            name=version_group_id,
            settings={
                "heartbeat_enabled": True,
                "lazarus_enabled": True,
                "version_locking_enabled": False,
            },
        ).insert()
    else:
        flow_group_id = flow_group.id

    version = (await models.Flow.where(version_where).max({"version"}))["version"] or 0

    # precompute task ids to make edges easy to add to database
    flow_id = await models.Flow(
        tenant_id=tenant_id,
        project_id=project_id,
        name=flow.name,
        serialized_flow=serialized_flow,
        environment=flow.environment,
        core_version=flow.environment.get("__version__"),
        storage=flow.storage,
        parameters=flow.parameters,
        version_group_id=version_group_id,
        version=version + 1,
        archived=False,
        flow_group_id=flow_group_id,
        description=description,
        schedule=serialized_flow.get("schedule"),
        is_schedule_active=set_schedule_active,
        tasks=[
            models.Task(
                id=t.id,
                tenant_id=tenant_id,
                name=t.name,
                slug=t.slug,
                type=t.type,
                max_retries=t.max_retries,
                tags=t.tags,
                retry_delay=t.retry_delay,
                trigger=t.trigger,
                mapped=t.mapped,
                auto_generated=t.auto_generated,
                cache_key=t.cache_key,
                is_reference_task=t.is_reference_task,
                is_root_task=t.is_root_task,
                is_terminal_task=t.is_terminal_task,
            )
            for t in flow.tasks
        ],
        edges=[
            models.Edge(
                tenant_id=tenant_id,
                upstream_task_id=task_lookup[e.upstream_task].id,
                downstream_task_id=task_lookup[e.downstream_task].id,
                key=e.key,
                mapped=e.mapped,
            )
            for e in flow.edges
        ],
    ).insert()

    # schedule runs
    if set_schedule_active:
        await schedule_flow_runs(flow_id=flow_id)

    return flow_id


@register_api("flows.delete_flow")
async def delete_flow(flow_id: str) -> bool:
    """
    Deletes a flow.

    Args:
        - flow_id (str): the flow id

    Returns:
        - bool: if the delete succeeded

    Raises:
        - ValueError: if a flow ID is not provided
    """
    if not flow_id:
        raise ValueError("Must provide flow ID.")

    # delete the flow
    result = await models.Flow.where(id=flow_id).delete()
    return bool(result.affected_rows)


@register_api("flows.archive_flow")
async def archive_flow(flow_id: str) -> bool:
    """
    Archives a flow.

    Archiving a flow prevents it from scheduling new runs. It also:
        - deletes any currently scheduled runs
        - resets the "last scheduled run time" of any schedules

    Args:
        - flow_id (str): the flow id

    Returns:
        - bool: if the update succeeded

    Raises:
        - ValueError: if a flow ID is not provided
    """
    if not flow_id:
        raise ValueError("Must provide flow ID.")

    result = await models.Flow.where({"id": {"_eq": flow_id}}).update(
        set={"archived": True}
    )
    if not result.affected_rows:
        return False

    # delete scheduled flow runs
    await models.FlowRun.where(
        {"flow_id": {"_eq": flow_id}, "state": {"_eq": "Scheduled"}}
    ).delete()

    return True


@register_api("flows.unarchive_flow")
async def unarchive_flow(flow_id: str) -> bool:
    """
    Unarchives a flow.

    Args:
        - flow_id (str): the flow id

    Returns:
        - bool: if the update succeeded

    Raises:
        - ValueError: if a flow ID is not provided
    """
    if not flow_id:
        raise ValueError("Must provide flow ID.")

    result = await models.Flow.where({"id": {"_eq": flow_id}}).update(
        set={"archived": False},
        selection_set={"affected_rows": True, "returning": {"is_schedule_active"}},
    )

    # if the schedule is active, jog it to trigger scheduling
    if result.affected_rows and result.returning[0].is_schedule_active:
        await set_schedule_active(flow_id)

    return bool(result.affected_rows)  # type: ignore


@register_api("flows.update_flow_project")
async def update_flow_project(flow_id: str, project_id: str) -> bool:
    """
    Updates a flow's project.

    Args:
        - flow_id (str): the flow id
        - project_id (str): the new project id

    Returns:
        - bool: if the update succeeded

    Raises:
        - ValueError: if flow or project IDs are not provided
    """
    if flow_id is None:
        raise ValueError("Invalid flow ID.")
    if project_id is None:
        raise ValueError("Invalid project ID.")

    result = await models.Flow.where(
        {"id": {"_eq": flow_id}, "tenant": {"projects": {"id": {"_eq": project_id}}}}
    ).update(dict(project_id=project_id))
    if not bool(result.affected_rows):
        raise ValueError("Invalid flow or project ID.")
    return flow_id


@register_api("flows.enable_heartbeat_for_flow")
async def enable_heartbeat_for_flow(flow_id: str) -> bool:
    """
    Enables heartbeats for a flow

    Args:
        - flow_id (str): the flow id

    Returns:
        - bool: if the update succeeded

    Raises:
        - ValueError: if flow ID is not provided or invalid
    """
    await api.flows._update_flow_setting(
        flow_id=flow_id, key="disable_heartbeat", value=False
    )
    await api.flows._update_flow_setting(
        flow_id=flow_id, key="heartbeat_enabled", value=True
    )

    return True


@register_api("flows.disable_heartbeat_for_flow")
async def disable_heartbeat_for_flow(flow_id: str) -> bool:
    """
    Disables heartbeats for a flow

    Args:
        - flow_id (str): the flow id

    Returns:
        - bool: if the update succeeded

    Raises:
        - ValueError: if flow ID is not provided or invalid
    """
    await api.flows._update_flow_setting(
        flow_id=flow_id, key="disable_heartbeat", value=True
    )
    await api.flows._update_flow_setting(
        flow_id=flow_id, key="heartbeat_enabled", value=False
    )
    return True


@register_api("flows.enable_lazarus_for_flow")
async def enable_lazarus_for_flow(flow_id: str) -> bool:
    """
    Enables lazarus for a flow

    Args:
        - flow_id (str): the flow id

    Returns:
        - bool: if the update succeeded

    Raises:
        - ValueError: if flow ID is not provided or invalid
    """
    await api.flows._update_flow_setting(
        flow_id=flow_id, key="lazarus_enabled", value=True
    )
    return True


@register_api("flows.disable_lazarus_for_flow")
async def disable_lazarus_for_flow(flow_id: str) -> bool:
    """
    Disables lazarus for a flow

    Args:
        - flow_id (str): the flow id

    Returns:
        - bool: if the update succeeded

    Raises:
        - ValueError: if flow ID is not provided or invalid
    """
    await api.flows._update_flow_setting(
        flow_id=flow_id, key="lazarus_enabled", value=False
    )
    return True


@register_api("flows.enable_version_locking_for_flow")
async def enable_version_locking_for_flow(flow_id: str) -> bool:
    """
    Enables version locking for a flow

    Args:
        - flow_id (str): the flow id

    Returns:
        - bool: if the update succeeded

    Raises:
        - ValueError: if flow ID is not provided or invalid
    """
    await api.flows._update_flow_setting(
        flow_id=flow_id, key="version_locking_enabled", value=True
    )
    return True


@register_api("flows.disable_version_locking_for_flow")
async def disable_version_locking_for_flow(flow_id: str) -> bool:
    """
    Disables version locking for a flow

    Args:
        - flow_id (str): the flow id

    Returns:
        - bool: if the update succeeded

    Raises:
        - ValueError: if flow ID is not provided or invalid
    """
    await api.flows._update_flow_setting(
        flow_id=flow_id, key="version_locking_enabled", value=False
    )
    return True


@register_api("flows.set_schedule_active")
async def set_schedule_active(flow_id: str) -> bool:
    """
    Sets a flow schedule to active

    Args:
        - flow_id (str): the flow ID

    Returns:
        bool: if the update succeeded
    """
    if flow_id is None:
        raise ValueError("Invalid flow id.")

    result = await models.Flow.where(id=flow_id).update(
        set={"is_schedule_active": True}
    )
    if not result.affected_rows:
        return False

    await schedule_flow_runs(flow_id=flow_id)
    return True


@register_api("flows.set_schedule_inactive")
async def set_schedule_inactive(flow_id: str) -> bool:
    """
    Sets a flow schedule to inactive

    Args:
        - flow_id (str): the flow ID

    Returns:
        bool: if the update succeeded
    """
    if flow_id is None:
        raise ValueError("Invalid flow id.")

    result = await models.Flow.where(id=flow_id).update(
        set={"is_schedule_active": False}
    )
    if not result.affected_rows:
        return False

    deleted_runs = await models.FlowRun.where(
        {
            "flow_id": {"_eq": flow_id},
            "state": {"_eq": "Scheduled"},
            "auto_scheduled": {"_eq": True},
        }
    ).delete()

    return True


@register_api("flows.schedule_flow_runs")
async def schedule_flow_runs(flow_id: str, max_runs: int = None) -> List[str]:
    """
    Schedule the next `max_runs` runs for this flow. Runs will not be scheduled
    if they are earlier than latest currently-scheduled run that has auto_scheduled = True.

    Runs are created with an idempotency key to avoid rescheduling.

    Args:
        - flow_id (str): the flow ID
        - max_runs (int): the maximum number of runs to schedule (defaults to 10)

    Returns:
        - List[str]: the ids of the new runs
    """

    if max_runs is None:
        max_runs = 10

    if flow_id is None:
        raise ValueError("Invalid flow id.")

    run_ids = []

    flow = await models.Flow.where(
        {
            # match the provided ID
            "id": {"_eq": flow_id},
            # schedule is not none or flow group schedule is not none
            "_or": [
                {"schedule": {"_is_null": False}},
                {"flow_group": {"schedule": {"_is_null": False}}},
            ],
            # schedule is active
            "is_schedule_active": {"_eq": True},
            # flow is not archived
            "archived": {"_eq": False},
        }
    ).first(
        {
            "schedule": True,
            "flow_group": {"schedule": True},
            with_args(
                "flow_runs_aggregate", {"where": {"auto_scheduled": {"_eq": True}}}
            ): {"aggregate": {"max": "scheduled_start_time"}},
        },
        apply_schema=False,
    )

    if not flow:
        logger.debug(f"Flow {flow_id} can not be scheduled.")
        return run_ids
    else:
        # attempt to pull the schedule from the flow group if possible
        if flow.flow_group.schedule:
            flow_schedule = flow.flow_group.schedule
        # if not possible, pull the schedule from the flow
        else:
            flow_schedule = flow.schedule
        try:
            flow_schedule = schedule_schema.load(flow_schedule)
        except Exception as exc:
            logger.error(exc)
            logger.critical(
                f"Failed to deserialize schedule for flow {flow_id}: {flow_schedule}"
            )
            return run_ids

    if flow.flow_runs_aggregate.aggregate.max.scheduled_start_time is not None:
        last_scheduled_run = pendulum.parse(
            flow.flow_runs_aggregate.aggregate.max.scheduled_start_time
        )
    else:
        last_scheduled_run = pendulum.now("UTC")

    # schedule every event with an idempotent flow run
    for event in flow_schedule.next(n=max_runs, return_events=True):

        # if this run was already scheduled, continue
        if last_scheduled_run and event.start_time <= last_scheduled_run:
            continue

        run_id = await api.runs.create_flow_run(
            flow_id=flow_id,
            scheduled_start_time=event.start_time,
            parameters=event.parameter_defaults,
            idempotency_key=f"auto-scheduled:{event.start_time.in_tz('UTC')}",
        )

        logger.debug(
            f"Flow run {run_id} of flow {flow_id} scheduled for {event.start_time}"
        )

        run_ids.append(run_id)

    await models.FlowRun.where({"id": {"_in": run_ids}}).update(
        set={"auto_scheduled": True}
    )

    return run_ids
