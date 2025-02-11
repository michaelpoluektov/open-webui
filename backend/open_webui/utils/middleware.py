import asyncio
import inspect
import json
import logging
import sys
from uuid import uuid4

from open_webui.constants import TASKS
from open_webui.env import (
    ENABLE_REALTIME_CHAT_SAVE,
    GLOBAL_LOG_LEVEL,
    SRC_LOG_LEVELS,
)
from open_webui.models.chats import Chats
from open_webui.models.functions import Functions
from open_webui.models.users import Users
from open_webui.routers.tasks import generate_title
from open_webui.socket.main import (
    get_active_status_by_user_id,
    get_event_call,
    get_event_emitter,
)
from open_webui.tasks import create_task
from open_webui.utils.misc import (
    get_message_list,
)
from open_webui.utils.plugin import load_function_module_by_id
from open_webui.utils.webhook import post_webhook
from starlette.responses import StreamingResponse

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


async def chat_completion_filter_functions_handler(request, body, model, extra_params):
    skip_files = None

    def get_filter_function_ids(model):
        def get_priority(function_id):
            function = Functions.get_function_by_id(function_id)
            if function is not None and hasattr(function, "valves"):
                # TODO: Fix FunctionModel
                return (function.valves if function.valves else {}).get("priority", 0)
            return 0

        filter_ids = [
            function.id for function in Functions.get_global_filter_functions()
        ]
        if "info" in model and "meta" in model["info"]:
            filter_ids.extend(model["info"]["meta"].get("filterIds", []))
            filter_ids = list(set(filter_ids))

        enabled_filter_ids = [
            function.id
            for function in Functions.get_functions_by_type("filter", active_only=True)
        ]

        filter_ids = [
            filter_id for filter_id in filter_ids if filter_id in enabled_filter_ids
        ]

        filter_ids.sort(key=get_priority)
        return filter_ids

    filter_ids = get_filter_function_ids(model)
    for filter_id in filter_ids:
        filter = Functions.get_function_by_id(filter_id)
        if not filter:
            continue

        if filter_id in request.app.state.FUNCTIONS:
            function_module = request.app.state.FUNCTIONS[filter_id]
        else:
            function_module, _, _ = load_function_module_by_id(filter_id)
            request.app.state.FUNCTIONS[filter_id] = function_module

        # Check if the function has a file_handler variable
        if hasattr(function_module, "file_handler"):
            skip_files = function_module.file_handler

        # Apply valves to the function
        if hasattr(function_module, "valves") and hasattr(function_module, "Valves"):
            valves = Functions.get_function_valves_by_id(filter_id)
            function_module.valves = function_module.Valves(
                **(valves if valves else {})
            )

        if hasattr(function_module, "inlet"):
            try:
                inlet = function_module.inlet

                # Create a dictionary of parameters to be passed to the function
                params = {"body": body} | {
                    k: v
                    for k, v in {
                        **extra_params,
                        "__model__": model,
                        "__id__": filter_id,
                    }.items()
                    if k in inspect.signature(inlet).parameters
                }

                if "__user__" in params and hasattr(function_module, "UserValves"):
                    try:
                        params["__user__"]["valves"] = function_module.UserValves(
                            **Functions.get_user_valves_by_id_and_user_id(
                                filter_id, params["__user__"]["id"]
                            )
                        )
                    except Exception as e:
                        print(e)

                if inspect.iscoroutinefunction(inlet):
                    body = await inlet(**params)
                else:
                    body = inlet(**params)

            except Exception as e:
                print(f"Error: {e}")
                raise e

    if skip_files and "files" in body.get("metadata", {}):
        del body["metadata"]["files"]

    return body, {}


def apply_params_to_form_data(form_data):
    params = form_data.pop("params", {})
    if "seed" in params:
        form_data["seed"] = params["seed"]

    if "stop" in params:
        form_data["stop"] = params["stop"]

    if "temperature" in params:
        form_data["temperature"] = params["temperature"]

    if "top_p" in params:
        form_data["top_p"] = params["top_p"]

    if "frequency_penalty" in params:
        form_data["frequency_penalty"] = params["frequency_penalty"]
    return form_data


async def process_chat_payload(request, form_data, metadata, user, model):
    form_data = apply_params_to_form_data(form_data)
    log.debug(f"form_data: {form_data}")

    event_emitter = get_event_emitter(metadata)
    event_call = get_event_call(metadata)

    extra_params = {
        "__event_emitter__": event_emitter,
        "__event_call__": event_call,
        "__user__": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
        "__metadata__": metadata,
        "__request__": request,
    }

    # Initialize events to store additional event to be sent to the client
    # Initialize contexts and citation
    events = []

    try:
        form_data, _ = await chat_completion_filter_functions_handler(
            request, form_data, model, extra_params
        )
    except Exception as e:
        raise Exception(f"Error: {e}")

    tool_ids = form_data.pop("tool_ids", None)
    files = form_data.pop("files", None)
    # Remove files duplicates
    if files:
        files = list({json.dumps(f, sort_keys=True): f for f in files}.values())

    metadata = {
        **metadata,
        "tool_ids": tool_ids,
        "files": files,
    }
    form_data["metadata"] = metadata
    return form_data, events


async def process_chat_response(
    request, response, form_data, user, events, metadata, tasks
):
    async def background_tasks_handler():
        message_map = Chats.get_messages_by_chat_id(metadata["chat_id"])
        message = message_map.get(metadata["message_id"]) if message_map else None
        if not message:
            return

        messages = get_message_list(message_map, message.get("id"))

        if not tasks:
            return
        if TASKS.TITLE_GENERATION in tasks:
            if tasks[TASKS.TITLE_GENERATION]:
                res = await generate_title(
                    request,
                    {
                        "model": message["model"],
                        "messages": messages,
                        "chat_id": metadata["chat_id"],
                    },
                    user,
                )

                if res and isinstance(res, dict):
                    title = (
                        res.get("choices", [])[0]
                        .get("message", {})
                        .get(
                            "content",
                            message.get("content", "New Chat"),
                        )
                    ).strip()

                    if not title:
                        title = messages[0].get("content", "New Chat")

                    Chats.update_chat_title_by_id(metadata["chat_id"], title)

                    if event_emitter:
                        await event_emitter(
                            {
                                "type": "chat:title",
                                "data": title,
                            }
                        )
            elif len(messages) == 2:
                title = messages[0].get("content", "New Chat")

                Chats.update_chat_title_by_id(metadata["chat_id"], title)
                if event_emitter:
                    await event_emitter(
                        {
                            "type": "chat:title",
                            "data": message.get("content", "New Chat"),
                        }
                    )

    event_emitter = None
    if (
        metadata.get("session_id")
        and metadata.get("chat_id")
        and metadata.get("message_id")
    ):
        event_emitter = get_event_emitter(metadata)

    if not isinstance(response, StreamingResponse):
        if not event_emitter:
            return response

        if "selected_model_id" in response:
            Chats.upsert_message_to_chat_by_id_and_message_id(
                metadata["chat_id"],
                metadata["message_id"],
                {
                    "selectedModelId": response["selected_model_id"],
                },
            )

        if response.get("choices", [])[0].get("message", {}).get("content"):
            content = response["choices"][0]["message"]["content"]

            if content:
                await event_emitter(
                    {
                        "type": "chat:completion",
                        "data": response,
                    }
                )

                title = Chats.get_chat_title_by_id(metadata["chat_id"])

                await event_emitter(
                    {
                        "type": "chat:completion",
                        "data": {
                            "done": True,
                            "content": content,
                            "title": title,
                        },
                    }
                )

                # Save message in the database
                Chats.upsert_message_to_chat_by_id_and_message_id(
                    metadata["chat_id"],
                    metadata["message_id"],
                    {
                        "content": content,
                    },
                )

                # Send a webhook notification if the user is not active
                if get_active_status_by_user_id(user.id) is None:
                    webhook_url = Users.get_user_webhook_url_by_id(user.id)
                    if webhook_url:
                        post_webhook(
                            webhook_url,
                            f"{title} - {request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}\n\n{content}",
                            {
                                "action": "chat",
                                "message": content,
                                "title": title,
                                "url": f"{request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}",
                            },
                        )

                await background_tasks_handler()

        return response

    if not any(
        content_type in response.headers["Content-Type"]
        for content_type in ["text/event-stream", "application/x-ndjson"]
    ):
        return response

    if not event_emitter:
        # Fallback to the original response
        async def stream_wrapper(original_generator, events):
            def wrap_item(item):
                return f"data: {item}\n\n"

            for event in events:
                yield wrap_item(json.dumps(event))

            async for data in original_generator:
                yield data

        return StreamingResponse(
            stream_wrapper(response.body_iterator, events),
            headers=dict(response.headers),
            background=response.background,
        )
    task_id = str(uuid4())  # Create a unique task ID.

    # Handle as a background task
    async def post_response_handler(response, events):
        message = Chats.get_message_by_id_and_message_id(
            metadata["chat_id"], metadata["message_id"]
        )
        content = message.get("content", "") if message else ""

        try:
            for event in events:
                await event_emitter(
                    {
                        "type": "chat:completion",
                        "data": event,
                    }
                )

                # Save message in the database
                Chats.upsert_message_to_chat_by_id_and_message_id(
                    metadata["chat_id"],
                    metadata["message_id"],
                    {
                        **event,
                    },
                )

            async for line in response.body_iterator:
                line = line.decode("utf-8") if isinstance(line, bytes) else line
                data = line

                # Skip empty lines
                if not data.strip():
                    continue

                # "data: " is the prefix for each event
                if not data.startswith("data: "):
                    continue

                # Remove the prefix
                data = data[len("data: ") :]

                try:
                    data = json.loads(data)

                    if "selected_model_id" in data:
                        Chats.upsert_message_to_chat_by_id_and_message_id(
                            metadata["chat_id"],
                            metadata["message_id"],
                            {
                                "selectedModelId": data["selected_model_id"],
                            },
                        )

                    else:
                        value = (
                            data.get("choices", [])[0].get("delta", {}).get("content")
                        )

                        if value:
                            content = f"{content}{value}"

                            if ENABLE_REALTIME_CHAT_SAVE:
                                # Save message in the database
                                Chats.upsert_message_to_chat_by_id_and_message_id(
                                    metadata["chat_id"],
                                    metadata["message_id"],
                                    {
                                        "content": content,
                                    },
                                )
                            else:
                                data = {
                                    "content": content,
                                }

                    await event_emitter(
                        {
                            "type": "chat:completion",
                            "data": data,
                        }
                    )

                except Exception as e:
                    done = "data: [DONE]" in line

                    if done:
                        pass
                    else:
                        continue

            title = Chats.get_chat_title_by_id(metadata["chat_id"])
            data = {"done": True, "content": content, "title": title}

            if not ENABLE_REALTIME_CHAT_SAVE:
                # Save message in the database
                Chats.upsert_message_to_chat_by_id_and_message_id(
                    metadata["chat_id"],
                    metadata["message_id"],
                    {
                        "content": content,
                    },
                )

            # Send a webhook notification if the user is not active
            if get_active_status_by_user_id(user.id) is None:
                webhook_url = Users.get_user_webhook_url_by_id(user.id)
                if webhook_url:
                    post_webhook(
                        webhook_url,
                        f"{title} - {request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}\n\n{content}",
                        {
                            "action": "chat",
                            "message": content,
                            "title": title,
                            "url": f"{request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}",
                        },
                    )

            await event_emitter(
                {
                    "type": "chat:completion",
                    "data": data,
                }
            )

            await background_tasks_handler()
        except asyncio.CancelledError:
            print("Task was cancelled!")
            await event_emitter({"type": "task-cancelled"})

            if not ENABLE_REALTIME_CHAT_SAVE:
                # Save message in the database
                Chats.upsert_message_to_chat_by_id_and_message_id(
                    metadata["chat_id"],
                    metadata["message_id"],
                    {
                        "content": content,
                    },
                )

        if response.background is not None:
            await response.background()

    # background_tasks.add_task(post_response_handler, response, events)
    task_id, _ = create_task(post_response_handler(response, events))
    return {"status": True, "task_id": task_id}
