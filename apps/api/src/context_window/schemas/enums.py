from enum import StrEnum


class Beat(StrEnum):
    RESEARCH = "research"
    RELEASES = "releases"
    INDUSTRY = "industry"
    COMMUNITY = "community"


class SourceKind(StrEnum):
    RSS = "rss"
    GITHUB_RELEASES = "github_releases"
    PYPI_RELEASES = "pypi_releases"
    ARXIV = "arxiv"
    HF_PAPERS = "hf_papers"
    HACKERNEWS = "hackernews"


class RunTrigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    RESUME_REQUESTED = "resume_requested"
    PUBLISHED = "published"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"


# At most one run per date may be in one of these states (enforced by a partial unique index).
ACTIVE_RUN_STATUSES: tuple[RunStatus, ...] = (
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.AWAITING_REVIEW,
    RunStatus.RESUME_REQUESTED,
)


class StoryStatus(StrEnum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    DROPPED = "dropped"
    PUBLISHED = "published"


class DropReason(StrEnum):
    UNSUPPORTED = "unsupported"
    UNCLEAR_ONLY = "unclear_only"
    FETCH_FAILED = "fetch_failed"
    BUDGET = "budget"
    REMOVED_BY_EDITOR = "removed_by_editor"


class Verdict(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNCLEAR = "unclear"


class EvidenceKind(StrEnum):
    PAGE = "page"
    FEED = "feed"


class BriefingStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
