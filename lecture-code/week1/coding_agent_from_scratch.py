# Step1 依赖和客户端
import inspect
import json
import os
import re
from pathlib import Path
from typing import Callable, Literal, TypedDict, cast

from dotenv import load_dotenv
from openai import OpenAI
from openai.types.responses import ResponseInputParam

_ = load_dotenv()

openai_client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    base_url=os.environ["OPENAI_BASE_URL"],
)

MODEL = "gpt-6.1-sol"
MAX_OUTPUT_TOKENS = 5000
# 模型把工具调用写成"叙述 + tool: ..."时，最多再纠正几次
MAX_FORMAT_RETRIES = 2
# AGENT_DEBUG=1 会打印模型原始输出（repr），用来排查不可见字符
DEBUG = os.environ.get("AGENT_DEBUG", "") not in ("", "0")

# 所有相对路径都以此为根。不依赖模型猜路径：模型经常把工作区目录名也拼进相对路径
WORKSPACE_ROOT = Path.cwd().resolve()


# Step2 路径解析
def resolve_abs_path(path_str: str) -> Path:
    """
    把模型给的路径解析成绝对路径，并限制在 WORKSPACE_ROOT 之内。
    """
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = WORKSPACE_ROOT / p
    p = p.resolve()
    if p != WORKSPACE_ROOT and WORKSPACE_ROOT not in p.parents:
        raise ValueError(f"path escapes workspace root {WORKSPACE_ROOT}: {path_str!r}")
    return p


# Step3 三个工具
def read_file_tool(filename: str) -> dict[str, str]:
    """
    Read a text file and return its full content.
    :param filename: path to the file, relative to the workspace root
    :return: {"file_path": absolute path, "content": file content}
    """
    full_path = resolve_abs_path(filename)
    return {
        "file_path": str(full_path),
        "content": full_path.read_text(encoding="utf-8"),
    }


class FileEntry(TypedDict):
    filename: str
    type: str


class FileListResult(TypedDict):
    path: str
    files: list[FileEntry]


def list_files_tool(path: str) -> FileListResult:
    """
    List the entries of a directory.
    :param path: path to the directory, relative to the workspace root; use "." for the root itself
    :return: {"path": absolute path, "files": [{"filename": name, "type": "file"|"dir"}]}
    """
    full_path = resolve_abs_path(path)

    if not full_path.is_dir():
        raise ValueError(f"not a directory: {full_path}")

    all_files: list[FileEntry] = []
    for item in sorted(full_path.iterdir()):
        all_files.append({
            "filename": item.name,
            "type": "file" if item.is_file() else "dir",
        })

    return {
        "path": str(full_path),
        "files": all_files,
    }


def edit_file_tool(path: str, old_str: str, new_str: str) -> dict[str, str]:
    """
    Replace the first occurrence of old_str with new_str in a file.
    :param path: path to the file, relative to the workspace root
    :param old_str: exact text to replace; if empty, the file is created/overwritten
    :param new_str: replacement text, or the whole file content when old_str is empty
    :return: {"path": absolute path, "action": create_file|replace|old_str not found}
    """
    p = resolve_abs_path(path)

    if old_str == "":
        _ = p.write_text(new_str, encoding="utf-8")
        return {
            "path": str(p),
            "action": "create_file",
        }

    original = p.read_text(encoding="utf-8")
    if old_str not in original:
        return {
            "path": str(p),
            "action": "old_str not found",
        }

    _ = p.write_text(original.replace(old_str, new_str, 1), encoding="utf-8")
    return {
        "path": str(p),
        "action": "replace",
    }


# Step4 工具注册表
TOOL_REGISTRY: dict[str, Callable[..., object]] = {
    "read_file": read_file_tool,
    "list_files": list_files_tool,
    "edit_file": edit_file_tool,
}


# Step5 自动生成工具说明书
def get_tool_str_representation(tool_name: str) -> str:
    tool = TOOL_REGISTRY[tool_name]
    doc = inspect.getdoc(tool) or ""
    return f"### {tool_name}{inspect.signature(tool)}\n{doc}\n"


# Step6 System Prompt
def create_full_system_prompt() -> str:
    tool_str_repr = ""
    for tool_name in TOOL_REGISTRY:
        tool_str_repr += get_tool_str_representation(tool_name) + "\n"
    return SYSTEM_PROMPT.format(
        tool_list_str=tool_str_repr,
        root=str(WORKSPACE_ROOT),
    )


# 规则前置 + 肯定式表述 + 两条真实示例，比末尾两句散文更能压住格式漂移
SYSTEM_PROMPT = """
You are a coding assistant with tools. Workspace root: {root}
All relative paths resolve from the workspace root. The root itself is ".".

## Output protocol (highest priority, overrides all other habits)
When you need a tool, your reply is exactly one line and nothing else:
tool: TOOL_NAME({{"arg":"value"}})

Rules for that line:
- It starts with "tool:" and nothing precedes it.
- It ends with ")" and nothing follows it.
- JSON is compact, single-line, double-quoted.
- You do not write an introduction, plan, apology, or explanation in the same reply.
- You do not use markdown or code fences.

Two correct replies:
tool: list_files({{"path":"."}})
tool: read_file({{"filename":"coding_agent_from_scratch.py"}})

When you do not need a tool, reply in normal prose.
After you receive a tool_result(...) message, continue the task.

## Tools
{tool_list_str}
"""

YOU_COLOR = "\u001b[94m"
ASSISTANT_COLOR = "\u001b[93m"
RESET_COLOR = "\u001b[0m"

# Step7 解析器
# 不再要求 tool: 出现在行首：模型经常把调用写在句子后面（"...files.tool: list_files(...)"），
# 或者把冒号写成全角。整段文本全局搜索，JSON 也允许跨行。
_TOOL_CALL_RE = re.compile(r"tool\s*[:：]\s*([A-Za-z_]\w*)\s*\(")
# 用来判断"模型想调工具但格式没解析出来"，避免把解析失败当成任务完成
_TOOL_INTENT_RE = re.compile(r"tool\s*[:：]|\b(?:list_files|read_file|edit_file)\s*\(")


def extract_tool_invocations(text: str) -> list[tuple[str, dict[str, object]]]:
    """
    Return every (tool_name, args) occurrence of 'tool: name({...})' in the reply.
    The call may be inline after prose, and the JSON may span multiple lines.
    """
    invocations: list[tuple[str, dict[str, object]]] = []

    for match in _TOOL_CALL_RE.finditer(text):
        name = match.group(1)
        rest = text[match.end():]
        try:
            # raw_decode 只解析到 JSON 结束，rest 里残留的 ')' 和后续文字会被忽略
            raw_args: object = cast(object, json.JSONDecoder().raw_decode(rest.lstrip())[0])
        except json.JSONDecodeError as e:
            print(f"[parse warning] {name}: bad JSON -> {e}")
            continue
        if not isinstance(raw_args, dict):
            print(f"[parse warning] {name}: args must be a JSON object")
            continue
        invocations.append((name, cast(dict[str, object], raw_args)))

    return invocations


def normalize_tool_calls(invocations: list[tuple[str, dict[str, object]]]) -> str:
    """把解析结果规范成单行调用，回写上下文时不会把模型的叙述式格式带进去。"""
    return "\n".join(
        f"tool: {name}({json.dumps(args, ensure_ascii=False)})"
        for name, args in invocations
    )


def looks_like_tool_attempt(text: str) -> bool:
    return bool(_TOOL_INTENT_RE.search(text))


REPROMPT = (
    "Your previous reply did not contain a parseable tool call, so no tool was executed. "
    "Reply with exactly one line and nothing else, in this form:\n"
    'tool: TOOL_NAME({"arg":"value"})\n'
    "Use compact single-line JSON with double quotes. "
    "Do not write any introduction, explanation, or apology."
)


# Step8 llm调用
Role = Literal["user", "assistant", "developer"]

# instructions 是 Responses API 的系统指令通道，比把 prompt 塞进对话数组更权威。
# 但第三方 base_url 代理不一定支持，所以失败一次就降级成 developer 消息。
_use_instructions = True


def _build_input(conversation: list[dict[str, str]]) -> ResponseInputParam:
    return [
        {
            "role": cast(Role, msg["role"]),
            "content": msg["content"],
        }
        for msg in conversation
    ]


def execute_llm_call(conversation: list[dict[str, str]], system_prompt: str) -> str:
    global _use_instructions

    if _use_instructions:
        messages: list[dict[str, str]] = list(conversation)
    else:
        # 降级模式：系统指令退化成对话里的第一条 developer 消息
        messages = [{"role": "developer", "content": system_prompt}] + list(conversation)

    try:
        if _use_instructions:
            response = openai_client.responses.create(
                model=MODEL,
                input=_build_input(messages),
                max_output_tokens=MAX_OUTPUT_TOKENS,
                instructions=system_prompt,
            )
        else:
            response = openai_client.responses.create(
                model=MODEL,
                input=_build_input(messages),
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
    except Exception as e:
        if not _use_instructions:
            raise
        print(f"[warn] instructions= 不被代理支持（{type(e).__name__}: {e}），降级为 developer 消息")
        _use_instructions = False
        return execute_llm_call(conversation, system_prompt)

    return response.output_text


# Step9 工具执行
def call_tool(name: str, args: dict[str, object]) -> object:
    """执行工具。任何失败都变成一条 tool_result 回给模型，而不是让进程崩掉。"""
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}", "available": sorted(TOOL_REGISTRY)}

    try:
        allowed = inspect.signature(fn).parameters
        kwargs = {k: v for k, v in args.items() if k in allowed}
        return fn(**kwargs)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# Step10 主循环
def run_coding_agent_loop() -> None:
    system_prompt = create_full_system_prompt()
    conversation: list[dict[str, str]] = []

    while True:
        try:
            user_msg = input(f"{YOU_COLOR}You{RESET_COLOR}: ")
        except (KeyboardInterrupt, EOFError):
            print()
            break

        if not user_msg.strip():
            continue

        conversation.append({"role": "user", "content": user_msg.strip()})
        format_retries = 0

        while True:
            assistant_resp = execute_llm_call(conversation, system_prompt)
            if DEBUG:
                print(f"[DEBUG] raw reply = {assistant_resp!r}")

            tool_invocations = extract_tool_invocations(assistant_resp)

            if not tool_invocations:
                # 解析失败不等于任务完成：只要模型看起来想调工具，就纠正它重发
                if looks_like_tool_attempt(assistant_resp) and format_retries < MAX_FORMAT_RETRIES:
                    format_retries += 1
                    print(f"[retry {format_retries}/{MAX_FORMAT_RETRIES}] 未解析到合法工具调用，要求模型重发")
                    conversation.append({"role": "user", "content": REPROMPT})
                    continue

                print(f"{ASSISTANT_COLOR}ASSISTANT{RESET_COLOR}: {assistant_resp.strip()}")
                conversation.append({
                    "role": "assistant",
                    "content": assistant_resp.strip(),
                })
                break

            format_retries = 0
            print(f"{ASSISTANT_COLOR}ASSISTANT{RESET_COLOR}: {assistant_resp.strip()}")
            # 只回写规范化的调用行，避免模型模仿自己上一轮的"叙述 + tool:"格式
            conversation.append({
                "role": "assistant",
                "content": normalize_tool_calls(tool_invocations),
            })

            for name, args in tool_invocations:
                print(f"  -> {name}({json.dumps(args, ensure_ascii=False)})")
                result = call_tool(name, args)
                conversation.append({
                    "role": "user",
                    "content": f"tool_result({json.dumps(result, ensure_ascii=False)})",
                })


if __name__ == "__main__":
    run_coding_agent_loop()
