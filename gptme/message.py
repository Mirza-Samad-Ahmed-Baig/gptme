import dataclasses
import hashlib
import logging
import shutil
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

import tomlkit
from rich.syntax import Syntax
from tomlkit._utils import escape_string
from typing_extensions import Self

from gptme.llm.models import get_default_model

from .codeblock import Codeblock
from .constants import ROLE_COLOR
from .util import console, get_tokenizer
from .util.prompt import rich_to_str

logger = logging.getLogger(__name__)


@dataclass(frozen=True, eq=False)
class Message:
    """
    A message in the assistant conversation.

    Attributes:
        role: The role of the message sender (system, user, or assistant).
        content: The content of the message.
        timestamp: The timestamp of the message.
        files: Files attached to the message, could e.g. be images for vision.
        pinned: Whether this message should be pinned to the top of the chat, and never context-trimmed.
        hide: Whether this message should be hidden from the chat output (but still be sent to the assistant).
        quiet: Whether this message should be printed on execution (will still print on resume, unlike hide).
               This is not persisted to the log file.
    """

    role: Literal["system", "user", "assistant"]
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    files: list[Path] = field(default_factory=list)
    call_id: str | None = None

    pinned: bool = False
    hide: bool = False
    quiet: bool = False

    def __post_init__(self):
        assert isinstance(self.timestamp, datetime)

    def __repr__(self):
        content = textwrap.shorten(self.content, 20, placeholder="...")
        return f"<Message role={self.role} content={content}>"

    def __eq__(self, other):
        # FIXME: really include timestamp?
        if not isinstance(other, Message):
            return False
        return (
            self.role == other.role
            and self.content == other.content
            and self.timestamp == other.timestamp
        )

    def replace(self, **kwargs) -> Self:
        """Replace attributes of the message."""
        return dataclasses.replace(self, **kwargs)

    def to_dict(self, keys=None) -> dict:
        """Return a dict representation of the message, serializable to JSON."""

        d: dict = {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.files:
            d["files"] = [str(f) for f in self.files]
        if self.pinned:
            d["pinned"] = True
        if self.hide:
            d["hide"] = True
        if self.call_id:
            d["call_id"] = self.call_id
        if keys:
            return {k: d[k] for k in keys if k in d}
        return d

    def to_xml(self) -> str:
        """Converts a message to an XML string."""
        attrs = f"role='{self.role}'"
        return f"<message {attrs}>\n{self.content}\n</message>"

    def format(
        self,
        oneline: bool = False,
        highlight: bool = False,
        max_length: int | None = None,
    ) -> str:
        """Format the message for display.

        Args:
            oneline: Whether to format the message as a single line
            highlight: Whether to highlight code blocks
            max_length: Maximum length of the message. If None, no truncation is applied.
                       If set, will truncate at first newline or max_length, whichever comes first.
        """
        if max_length is not None:
            first_newline = self.content.find("\n")
            max_length = (
                min(max_length, first_newline) if first_newline != -1 else max_length
            )
            content = self.content[:max_length]
            if len(content) < len(self.content):
                content += "..."
            temp_msg = self.replace(content=content)
            return format_msgs([temp_msg], oneline=True, highlight=highlight)[0]
        return format_msgs([self], oneline=oneline, highlight=highlight)[0]

    def print(self, oneline: bool = False, highlight: bool = True) -> None:
        print_msg(self, oneline=oneline, highlight=highlight)

    def to_toml(self) -> str:
        """Converts a message to a TOML string, for easy editing by hand in editor to then be parsed back."""
        flags = []
        if self.pinned:
            flags.append("pinned")
        if self.hide:
            flags.append("hide")
        flags_toml = "\n".join(f"{flag} = true" for flag in flags)
        files_toml = f"files = {[str(f) for f in self.files]}" if self.files else ""
        extra = (flags_toml + "\n" + files_toml).strip()

        # doublequotes need to be escaped
        # content = self.content.replace('"', '\\"')
        content = escape_string(self.content)
        content = content.replace("\\n", "\n")
        content = content.strip()

        return f'''[message]
role = "{self.role}"
content = """
{content}
"""
timestamp = "{self.timestamp.isoformat()}"
call_id = "{self.call_id}"
{extra}
'''

    @classmethod
    def from_toml(cls, toml: str) -> Self:
        """
        Converts a TOML string to a message.

        The string can be a single [[message]].
        """

        t = tomlkit.parse(toml)
        assert "message" in t and isinstance(t["message"], dict)
        msg: dict = t["message"]  # type: ignore

        return cls(
            msg["role"],
            msg["content"].strip(),
            pinned=msg.get("pinned", False),
            hide=msg.get("hide", False),
            files=[Path(f) for f in msg.get("files", [])],
            timestamp=datetime.fromisoformat(msg["timestamp"]),
            call_id=msg.get("call_id", None),
        )

    def get_codeblocks(self) -> list[Codeblock]:
        """
        Get all codeblocks from the message content.
        """
        content_str = self.content

        # prepend newline to make sure we get the first codeblock
        if not content_str.startswith("\n"):
            content_str = "\n" + content_str

        # check if message contains a code block
        backtick_count = content_str.count("\n```")
        if backtick_count < 2:
            return []

        return Codeblock.iter_from_markdown(content_str)

    def cost(self, model: str | None = None, output=False) -> float:
        """Get the input cost of the message in USD."""
        from .llm.models import get_model  # noreorder

        m = get_model(model) if model else get_default_model()
        assert m, "No model specified or loaded"
        tok = len_tokens(self, f"{m.provider}/{m.model}")
        price = (m.price_output if output else m.price_input) / 1_000_000
        return tok * price


def format_msgs(
    msgs: list[Message],
    oneline: bool = False,
    highlight: bool = False,
    indent: int = 0,
) -> list[str]:
    """Formats messages for printing to the console."""
    outputs = []
    for msg in msgs:
        userprefix = msg.role.capitalize()
        if highlight:
            color = ROLE_COLOR[msg.role]
            userprefix = f"[bold {color}]{userprefix}[/bold {color}]"

        # get terminal width
        max_len = shutil.get_terminal_size().columns - len(userprefix)
        output = ""
        if oneline:
            output += textwrap.shorten(
                msg.content.replace("\n", "\\n"), width=max_len, placeholder="..."
            )
            if len(output) < 20:
                output = msg.content.replace("\n", "\\n")[:max_len] + "..."
        else:
            multiline = len(msg.content.split("\n")) > 1
            output += "\n" + indent * " " if multiline else ""
            for i, block in enumerate(msg.content.split("```")):
                if i % 2 == 0:
                    output += textwrap.indent(block, prefix=indent * " ")
                    continue
                elif highlight:
                    lang = block.split("\n", 1)[0]
                    content = block.split("\n", 1)[-1]
                    fmt = "underline blue"
                    block = f"[{fmt}]{lang}\n[/{fmt}]" + rich_to_str(
                        Syntax(
                            content.rstrip().replace("[", r"\["),
                            lang,
                        )
                    )
                output += f"```{block.rstrip()}\n```"

        status_emoji = ""
        if msg.role == "system":
            first_line = msg.content.split("\n", 1)[0].lower()
            isSuccess = first_line.startswith(
                ("saved", "appended", "patch successfully")
            )
            isError = first_line.startswith(("error", "failed"))
            if isSuccess:
                status_emoji = "✅ "
            elif isError:
                status_emoji = "❌ "

        outputs.append(f"{userprefix}: {status_emoji}{output.rstrip()}")
    return outputs


def print_msg(
    msg: Message | list[Message],
    oneline: bool = False,
    highlight: bool = True,
    show_hidden: bool = False,
) -> None:
    """Prints the log to the console."""
    # if not tty, force highlight=False (for tests and such)
    if not sys.stdout.isatty():
        highlight = False

    msgs = msg if isinstance(msg, list) else [msg]
    msgstrs = format_msgs(msgs, highlight=highlight, oneline=oneline)
    skipped_hidden = 0
    for m, s in zip(msgs, msgstrs):
        if m.hide and not show_hidden:
            skipped_hidden += 1
            continue
        try:
            console.print(s)
        except Exception:
            # rich can throw errors, if so then print the raw message
            logger.exception("Error printing message")
            print(s)
    if skipped_hidden:
        console.print(
            f"[grey30]Skipped {skipped_hidden} hidden system messages, show with --show-hidden[/]"
        )


def msgs_to_toml(msgs: list[Message]) -> str:
    """Converts a list of messages to a TOML string, for easy editing by hand in editor to then be parsed back."""
    t = ""
    for msg in msgs:
        t += msg.to_toml().replace("[message]", "[[messages]]") + "\n\n"

    return t


def toml_to_msgs(toml: str) -> list[Message]:
    """
    Converts a TOML string to a list of messages.

    The string can be a whole file with multiple [[messages]].
    """
    t = tomlkit.parse(toml)
    assert "messages" in t and isinstance(t["messages"], list)
    msgs: list[dict] = t["messages"]  # type: ignore

    return [
        Message(
            msg["role"],
            msg["content"].strip(),
            pinned=msg.get("pinned", False),
            hide=msg.get("hide", False),
            files=[Path(f) for f in msg.get("files", [])],
            timestamp=datetime.fromisoformat(msg["timestamp"]),
            call_id=msg.get("call_id", None),
        )
        for msg in msgs
    ]


def msgs2dicts(msgs: list[Message]) -> list[dict]:
    """Convert a list of Message objects to a list of dicts ready to pass to an LLM."""
    return [msg.to_dict(keys=["role", "content", "files", "call_id"]) for msg in msgs]


# Global cache mapping hashes to token counts
_token_cache: dict[tuple[str, str], int] = {}


def _hash_content(content: str) -> str:
    """Create a hash of the content"""
    return hashlib.sha256(content.encode()).hexdigest()


def len_tokens(content: str | Message | list[Message], model: str) -> int:
    """Get the number of tokens in a string, message, or list of messages.

    Uses efficient caching with content hashing to minimize memory usage while
    maintaining fast repeated calculations, which is especially important for
    conversations with many messages.
    """
    if isinstance(content, list):
        return sum(len_tokens(msg, model) for msg in content)
    if isinstance(content, Message):
        content = content.content

    assert isinstance(content, str), content
    # Check cache using hash
    content_hash = _hash_content(content)
    cache_key = (content_hash, model)
    if cache_key in _token_cache:
        return _token_cache[cache_key]

    # Calculate and cache
    count = len(get_tokenizer(model).encode(content))
    _token_cache[cache_key] = count

    # Limit cache size by removing oldest entries if needed
    if len(_token_cache) > 1000:
        # Remove first item (oldest in insertion order)
        _token_cache.pop(next(iter(_token_cache)))

    return count
