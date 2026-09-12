"""Conservative classification of messages returned by Dola.

Only explicit restriction messages may change account availability.  Everything
else is task-local so an ambiguous translation cannot silently disable an
account.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


CONTENT_REJECTION_JA = "ご希望のコンテンツを生成できません。他の内容をお試しください。"
RESTRICTION_KINDS = frozenset({"daily_limit", "credits_unavailable", "risk_control"})


@dataclass(frozen=True)
class UpstreamClassification:
    kind: str
    text: str

    @property
    def is_restriction(self) -> bool:
        return self.kind in RESTRICTION_KINDS


_DAILY_LIMIT = re.compile(
    r"動画生成の\s*1日あたりの上限|画像生成の\s*1日あたりの上限|"
    r"每日(?:视频|影片|图像|圖片)?生成.{0,40}(?:上限|限额|額度)|"
    r"(?:daily|per[ -]?day).{0,50}(?:limit|quota)|"
    r"(?:limit|quota).{0,50}(?:daily|per[ -]?day)",
    re.IGNORECASE,
)
_CREDITS = re.compile(
    r"(?:insufficient|not enough|unavailable|exhausted|zero|no)\s+"
    r"(?:credits?|points?|balance)|"
    r"(?:credits?|points?|balance).{0,25}(?:insufficient|unavailable|exhausted|empty)|"
    r"(?:余额|餘額|残高|积分|積分|ポイント).{0,15}(?:不足|使えません|利用できません|已用完|耗尽|耗盡)",
    re.IGNORECASE,
)
_RISK = re.compile(
    r"risk\s*control|too\s+many\s+requests|requests?.{0,20}too\s+frequent|"
    r"rate\s*limit(?:ed)?|操作(?:过于|過於)?频繁|请求(?:过于|過於)?频繁|"
    r"リクエスト.{0,20}(?:多すぎ|頻繁)|71002200[24]",
    re.IGNORECASE,
)
_CREDITS_NOT_USED = re.compile(
    r"credits?\s+(?:were\s+)?not\s+(?:used|charged|deducted)|"
    r"(?:ポイント|クレジット).{0,20}(?:消費されません|使用されません)",
    re.IGNORECASE,
)


def classify_upstream_text(value: object) -> UpstreamClassification:
    """Classify upstream text using strict, safety-first precedence."""
    text = str(value or "").strip()
    # This exact rejection has historically been wrapped in a misleading local
    # "Insufficient quota:" prefix, so a substring check intentionally wins.
    if CONTENT_REJECTION_JA in text:
        return UpstreamClassification("content_rejected", text)
    if _DAILY_LIMIT.search(text):
        return UpstreamClassification("daily_limit", text)
    # A reassurance that credits were not consumed is not a credit shortage.
    if not _CREDITS_NOT_USED.search(text) and _CREDITS.search(text):
        return UpstreamClassification("credits_unavailable", text)
    if _RISK.search(text):
        return UpstreamClassification("risk_control", text)
    return UpstreamClassification("temporary", text)


class PromptContentRejectedError(RuntimeError):
    """The prompt was rejected; the account remains available."""


class DolaTemporarilyUnavailableError(RuntimeError):
    """Ambiguous upstream failure; the account remains available."""


class VerificationRequiredError(RuntimeError):
    """Dola requires a user verification step; never an account restriction."""


class ExplicitRestrictionError(RuntimeError):
    """Dola explicitly reported a persistent account/media restriction."""

    def __init__(self, kind: str, detail: str, *, pre_generation: bool = True):
        if kind not in RESTRICTION_KINDS:
            raise ValueError(f"Unsupported restriction kind: {kind}")
        self.kind = kind
        self.detail = detail
        self.pre_generation = pre_generation
        super().__init__(detail)


def error_from_upstream_text(value: object, *, pre_generation: bool = True) -> RuntimeError:
    result = classify_upstream_text(value)
    if result.kind == "content_rejected":
        return PromptContentRejectedError(
            "Dola rejected this content. Change the prompt and try again."
        )
    if result.is_restriction:
        return ExplicitRestrictionError(
            result.kind, result.text or "Dola reported an account restriction",
            pre_generation=pre_generation,
        )
    return DolaTemporarilyUnavailableError(
        "Dola is temporarily unavailable. This account was not restricted; try again later."
    )
