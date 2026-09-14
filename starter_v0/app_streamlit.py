"""Streamlit Live Chat Interface for IT Helpdesk Agent.

Supports interactive multi-turn conversations, tool calling inspection,
artifact version tracking, and transcript export.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure starter_v0 directory is in sys.path for local module imports
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st
import os

from env_loader import load_lab_env
from providers import make_provider
from providers.base import ToolCall
from tools import TOOL_FUNCTIONS, load_tool_declarations, to_openai_tools
from versioning import artifact_version_dict, build_artifact_version

ARTIFACTS_DIR = ROOT / "artifacts"
TRANSCRIPTS_DIR = ROOT / "transcripts"
load_lab_env(ROOT)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_slug(value: str) -> str:
    import re
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return slug.strip("_") or "run"


def trim_history(history: list[dict[str, str]], window: int) -> list[dict[str, str]]:
    if window <= 0:
        return []
    return history[-window * 2:]


def execute_tool_call(call: ToolCall) -> dict[str, Any]:
    func = TOOL_FUNCTIONS.get(call.name)
    if not func:
        return {
            "tool": call.name,
            "args": call.args,
            "result": {"error": "unknown_tool", "message": f"No local implementation for {call.name}"},
        }
    try:
        result = func(**call.args)
    except Exception as exc:
        result = {"error": type(exc).__name__, "message": str(exc)}
    return {"tool": call.name, "args": call.args, "result": result}


def tool_results_message(events: list[dict[str, Any]]) -> dict[str, str]:
    text = json.dumps(events, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) > 24000:
        text = text[:24000] + "\n...<truncated>"
    return {
        "role": "user",
        "content": (
            "TOOL_RESULTS_JSON:\n"
            f"{text}\n\n"
            "Use only these tool results. If the user asked for an incident report and the findings are ready, "
            "call the reporting tool. Otherwise answer directly, state uncertainty, and give the safest next step."
        ),
    }


def assistant_tool_message(response_text: str | None, calls: list[ToolCall]) -> dict[str, str]:
    call_summary = [{"name": call.name, "args": call.args} for call in calls]
    content = response_text or "I will call the selected tool(s)."
    return {
        "role": "assistant",
        "content": f"{content}\n\nTOOL_CALLS_JSON:\n{json.dumps(call_summary, ensure_ascii=False, separators=(',', ':'))}",
    }


def run_model_tool_loop_ui(
    *,
    provider: Any,
    messages: list[dict[str, str]],
    tools: list[dict[str, Any]],
    model: str | None,
    max_tool_rounds: int,
) -> dict[str, Any]:
    working_messages = list(messages)
    rounds: list[dict[str, Any]] = []
    all_tool_events: list[dict[str, Any]] = []

    for round_index in range(1, max_tool_rounds + 1):
        response = provider.complete(working_messages, tools, model=model, temperature=0.0)
        calls = response.tool_calls
        round_record: dict[str, Any] = {
            "round": round_index,
            "assistant_text": response.text,
            "tool_calls": [{"name": call.name, "args": call.args} for call in calls],
            "tool_results": [],
        }

        if not calls:
            rounds.append(round_record)
            return {
                "status": "answered",
                "assistant_text": response.text or "",
                "rounds": rounds,
                "tool_events": all_tool_events,
            }

        working_messages.append(assistant_tool_message(response.text, calls))
        non_clarification_events: list[dict[str, Any]] = []

        for call in calls:
            event = execute_tool_call(call)
            round_record["tool_results"].append(event)
            all_tool_events.append(event)

            result = event.get("result", {})
            if isinstance(result, dict) and result.get("awaiting_user"):
                question = result.get("question") or call.args.get("question") or "Bạn bổ sung thêm thông tin nhé."
                rounds.append(round_record)
                return {
                    "status": "waiting_for_user",
                    "assistant_text": question,
                    "rounds": rounds,
                    "tool_events": all_tool_events,
                }

            non_clarification_events.append(event)

        rounds.append(round_record)
        working_messages.append(tool_results_message(non_clarification_events))
        # Pacing delay between tool rounds to prevent burst rate-limit (429) on Gemini API
        import time
        time.sleep(1.2)

    return {
        "status": "max_tool_rounds",
        "assistant_text": f"Stopped after {max_tool_rounds} tool rounds. Inspect the transcript for details.",
        "rounds": rounds,
        "tool_events": all_tool_events,
    }


def write_transcript(path: Path, transcript: dict[str, Any]) -> None:
    transcript["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# ==============================================================================
# Streamlit Application
# ==============================================================================
st.set_page_config(
    page_title="IT Helpdesk Agent — Live Chat",
    page_icon="🛠️",
    layout="wide",
)

# ----------------- SIDEBAR CONFIGURATION -----------------
with st.sidebar:
    st.header("⚙️ Agent Configuration")

    provider_choice = st.selectbox(
        "Provider",
        options=["gemini", "openai", "anthropic", "openrouter"],
        index=0,
        help="Select live LLM provider adapter",
    )

    default_models = {
        "gemini": "gemini-3.1-flash-lite",
        "openai": "gpt-4o-mini",
        "anthropic": "claude-3-5-haiku-latest",
        "openrouter": "google/gemini-3.1-flash-lite",
    }
    model_input = st.text_input(
        "Model Name",
        value=default_models.get(provider_choice, "gemini-3.1-flash-lite"),
        key=f"model_input_{provider_choice}",
        help="Model ID to invoke",
    )

    api_key_env_names = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }
    target_env = api_key_env_names.get(provider_choice, "API_KEY")
    current_key = os.getenv(target_env, "")
    api_key_input = st.text_input(
        f"{provider_choice.capitalize()} API Key",
        value=current_key,
        type="password",
        help=f"Loaded from {target_env} or enter manually here",
    )
    if api_key_input:
        os.environ[target_env] = api_key_input

    version_choice = st.selectbox(
        "Artifact Version Label",
        options=["v0", "v1", "v2", "v3"],
        index=0,
        help="Track which experimental version is running",
    )

    prompt_path_input = st.text_input(
        "System Prompt File",
        value=str(ARTIFACTS_DIR / "system_prompt.md"),
    )
    tools_path_input = st.text_input(
        "Tools Declarations YAML",
        value=str(ARTIFACTS_DIR / "tools.yaml"),
    )

    history_window = st.slider("History Window (turns)", min_value=1, max_value=10, value=5)
    max_tool_rounds = st.slider("Max Tool Rounds", min_value=1, max_value=8, value=4)

    # Compute hashes & artifact version
    prompt_path = Path(prompt_path_input)
    tools_path = Path(tools_path_input)

    if prompt_path.exists() and tools_path.exists():
        current_artifact_version = build_artifact_version(version_choice, prompt_path, tools_path)
        st.caption(f"🏷️ **Artifact Tag:** `{current_artifact_version.artifact_version}`")
        with st.expander("🔍 View Artifact Hashes"):
            st.text(f"Prompt: {current_artifact_version.prompt_hash[:16]}...")
            st.text(f"Tools:  {current_artifact_version.tools_hash[:16]}...")
    else:
        st.error("Prompt or Tools file path does not exist!")
        current_artifact_version = None

    st.divider()

    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        if st.button("🔄 Reset Chat", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    # Sample demo queries for quick testing
    st.markdown("### 💡 Quick Test Scenarios")
    sample_queries = [
        "Kiểm tra tình trạng mạng Wi-Fi và VPN công ty.",
        "Thiết bị LT-204 của tôi bị lỗi VPN, hãy kiểm tra giúp tôi.",
        "Tôi không vào được máy tính, kiểm tra giúp tôi với!",
        "Tra cứu thông tin nhân viên EMP-101.",
        "Tạo ticket xin cấp quyền VPN cho tôi.",
    ]
    for q in sample_queries:
        if st.button(q, key=f"quick_{q[:15]}", use_container_width=True):
            st.session_state["queued_query"] = q
            st.rerun()


# ----------------- SESSION STATE INITIALIZATION -----------------
if "chat_messages" not in st.session_state:
    st.session_state["chat_messages"] = []  # list of {role, content, turn_record}
if "history" not in st.session_state:
    st.session_state["history"] = []  # raw history [{role, content}]
if "turn_index" not in st.session_state:
    st.session_state["turn_index"] = 0

if "transcript" not in st.session_state and current_artifact_version:
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    transcript_id = "_".join([
        safe_slug(version_choice),
        safe_slug(provider_choice),
        timestamp,
    ])
    st.session_state["transcript_path"] = TRANSCRIPTS_DIR / f"{transcript_id}.transcript.json"
    st.session_state["transcript"] = {
        "transcript_id": transcript_id,
        **artifact_version_dict(current_artifact_version),
        "provider": provider_choice,
        "model": model_input or provider_choice,
        "system_prompt": str(prompt_path),
        "tools": str(tools_path),
        "history_window": history_window,
        "max_tool_rounds": max_tool_rounds,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "turns": [],
    }

# ----------------- MAIN CHAT UI -----------------
st.title("🛠️ IT Helpdesk Agent — Live Chat")
if current_artifact_version:
    st.caption(
        f"**Active Version:** `{current_artifact_version.artifact_version}` | "
        f"**Provider:** `{provider_choice}` | "
        f"**Model:** `{model_input or 'default'}`"
    )

# Render existing messages
for msg in st.session_state["chat_messages"]:
    role = msg["role"]
    with st.chat_message(role):
        st.markdown(msg["content"])
        turn_record = msg.get("turn_record")
        if turn_record and turn_record.get("tool_events"):
            with st.expander(f"🔧 Tool Execution ({len(turn_record['tool_events'])} call(s)) — Status: `{turn_record.get('status')}`"):
                for round_info in turn_record.get("rounds", []):
                    st.markdown(f"**Round {round_info['round']}**")
                    for tc in round_info.get("tool_calls", []):
                        st.markdown(f"👉 **Call:** `{tc['name']}`")
                        st.json(tc.get("args", {}))
                    for tr in round_info.get("tool_results", []):
                        res = tr.get("result", {})
                        if isinstance(res, dict) and "error" in res:
                            st.error(f"❌ **Result ({tr['tool']}):** {res['error']}")
                            st.json(res)
                        else:
                            st.success(f"✅ **Result ({tr['tool']}):**")
                            st.json(res)

# Handling User Input (via chat_input or quick button)
user_prompt = st.chat_input("Nhập yêu cầu cần trợ giúp IT...")
if "queued_query" in st.session_state:
    user_prompt = st.session_state.pop("queued_query")

if user_prompt:
    if not prompt_path.exists() or not tools_path.exists():
        st.error("Không tìm thấy file system_prompt.md hoặc tools.yaml.")
        st.stop()

    system_prompt = prompt_path.read_text(encoding="utf-8")
    tool_declarations = load_tool_declarations(tools_path)
    openai_tools = to_openai_tools(tool_declarations)

    try:
        provider = make_provider(provider_choice)
    except Exception as exc:
        st.error(f"Lỗi khởi tạo provider `{provider_choice}`: {exc}")
        st.stop()

    selected_model = model_input.strip() if model_input.strip() else getattr(provider, "default_model", None)

    # 1. Display and record user message
    st.session_state["turn_index"] += 1
    current_turn = st.session_state["turn_index"]
    st.session_state["chat_messages"].append({"role": "user", "content": user_prompt})

    with st.chat_message("user"):
        st.markdown(user_prompt)

    # 2. Build message history
    trimmed_context = trim_history(st.session_state["history"], history_window)
    messages = [
        {"role": "system", "content": system_prompt},
        *trimmed_context,
        {"role": "user", "content": user_prompt},
    ]

    turn_record = {
        "turn_index": current_turn,
        "started_at": now_iso(),
        "user": user_prompt,
        "status": "started",
        "assistant_text": None,
        "rounds": [],
        "tool_events": [],
    }

    # 3. Execute model-tool loop with progress feedback
    with st.chat_message("assistant"):
        with st.spinner("🤖 Agent đang xử lý và thực thi công cụ..."):
            try:
                loop_result = run_model_tool_loop_ui(
                    provider=provider,
                    messages=messages,
                    tools=openai_tools,
                    model=selected_model,
                    max_tool_rounds=max_tool_rounds,
                )
                turn_record.update(loop_result)
                assistant_text = loop_result["assistant_text"]
            except Exception as exc:
                err_msg = f"{type(exc).__name__}: {str(exc)}"
                turn_record.update({
                    "status": "provider_error",
                    "error": err_msg,
                    "assistant_text": f"⚠️ Có lỗi xảy ra trong quá trình xử lý: {err_msg}",
                })
                assistant_text = turn_record["assistant_text"]

        st.markdown(assistant_text)

        # Display tool call inspection expander
        if turn_record.get("tool_events"):
            with st.expander(f"🔧 Tool Execution ({len(turn_record['tool_events'])} call(s)) — Status: `{turn_record.get('status')}`", expanded=True):
                for round_info in turn_record.get("rounds", []):
                    st.markdown(f"**Round {round_info['round']}**")
                    for tc in round_info.get("tool_calls", []):
                        st.markdown(f"👉 **Call:** `{tc['name']}`")
                        st.json(tc.get("args", {}))
                    for tr in round_info.get("tool_results", []):
                        res = tr.get("result", {})
                        if isinstance(res, dict) and "error" in res:
                            st.error(f"❌ **Result ({tr['tool']}):** {res['error']}")
                            st.json(res)
                        else:
                            st.success(f"✅ **Result ({tr['tool']}):**")
                            st.json(res)

    turn_record["ended_at"] = now_iso()

    # 4. Save state and transcripts
    st.session_state["history"].append({"role": "user", "content": user_prompt})
    st.session_state["history"].append({"role": "assistant", "content": assistant_text})
    st.session_state["chat_messages"].append({
        "role": "assistant",
        "content": assistant_text,
        "turn_record": turn_record,
    })

    if "transcript" in st.session_state:
        st.session_state["transcript"]["turns"].append(turn_record)
        write_transcript(st.session_state["transcript_path"], st.session_state["transcript"])

# ----------------- SIDEBAR TRANSCRIPT EXPORT -----------------
with st.sidebar:
    st.divider()
    st.markdown("### 📄 Transcript Evidence")
    if "transcript" in st.session_state and st.session_state["transcript"]["turns"]:
        st.caption(f"💾 **Saved to:** `{st.session_state['transcript_path'].name}`")
        transcript_json_str = json.dumps(st.session_state["transcript"], ensure_ascii=False, indent=2)
        st.download_button(
            label="⬇️ Tải file Transcript JSON",
            data=transcript_json_str,
            file_name=st.session_state["transcript_path"].name,
            mime="application/json",
            use_container_width=True,
        )
    else:
        st.caption("Chưa có lượt hội thoại nào được ghi lại.")
