import typing
from enum import Enum, auto
from dataclasses import dataclass

from kodiak import config
from kodiak.config import MergeMethod
from kodiak.queries import (
    PullRequest,
    PullRequestState,
    MergeStateStatus,
    RepoInfo,
    MergableState,
    BranchProtectionRule,
    PRReview,
    PRReviewState,
    StatusContext,
    StatusState,
)
import structlog

log = structlog.get_logger()


class MergeErrors(str, Enum):
    MISSING_WHITELIST_LABEL = auto()
    MISSING_BLACKLIST_LABEL = auto()
    PR_MERGED = auto()
    PR_CLOSED = auto()
    # there are unsuccessful checks
    UNSTABLE_MERGE = auto()
    DRAFT = auto()
    DIRTY = auto()
    BLOCKED = auto()
    UNEXPECTED_VALUE = auto()


async def valid_merge_methods(cfg: config.V1, repo: RepoInfo) -> bool:
    if cfg.merge.method == config.MergeMethod.merge:
        return repo.merge_commit_allowed
    if cfg.merge.method == config.MergeMethod.squash:
        return repo.squash_merge_allowed
    if cfg.merge.method == config.MergeMethod.rebase:
        return repo.rebase_merge_allowed
    raise TypeError("Unknown value")


class Queueable(BaseException):
    pass


class MissingGithubMergabilityState(Queueable):
    """Github hasn't evaluated if this PR can be merged without conflicts yet"""


class NeedsBranchUpdate(Queueable):
    pass


class WaitingForChecks(Queueable):
    pass


class NotQueueable(BaseException):
    pass


def mergable(
    config: config.V1,
    pull_request: PullRequest,
    branch_protection: BranchProtectionRule,
    reviews: typing.List[PRReview],
    contexts: typing.List[StatusContext],
    valid_signature: bool,
    valid_merge_methods: typing.List[MergeMethod],
) -> None:
    if config.merge.whitelist:
        if set(pull_request.labels).isdisjoint(set(config.merge.whitelist)):
            log.info(
                "missing required whitelist labels",
                has=pull_request.labels,
                requires=config.merge.whitelist,
            )
            raise NotQueueable("missing whitelist")
    if config.merge.blacklist:
        if not set(pull_request.labels).isdisjoint(config.merge.blacklist):
            log.info("missing required blacklist labels")
            raise NotQueueable("has blacklist labels")

    if config.merge.method not in valid_merge_methods:
        # TODO: This is a fatal configuration error. We should provide some notification of this issue
        log.error(
            "invalid configuration. Merge method not possible",
            configured_merge_method=config.merge.method,
            valid_merge_methods=valid_merge_methods,
        )
        raise NotQueueable("invalid merge methods")

    if pull_request.state == PullRequestState.MERGED:
        raise NotQueueable("merged")
    if pull_request.state == PullRequestState.CLOSED:
        raise NotQueueable("closed")
    if (
        pull_request.mergeStateStatus == MergeStateStatus.DIRTY
        or pull_request.mergeable == MergableState.CONFLICTING
    ):
        raise NotQueueable("merge conflict")

    if pull_request.mergeStateStatus == MergeStateStatus.UNSTABLE:
        # TODO: This status means that the pr is mergeable but has failing
        # status checks. we may want to handle this via config
        pass

    if pull_request.mergeable == MergableState.UNKNOWN:
        # we need to trigger a test commit to fix this. We do that by calling
        # GET on the pull request endpoint.
        raise MissingGithubMergabilityState("missing mergeablity state")

    if (
        pull_request.mergeStateStatus == MergeStateStatus.BEHIND
        and branch_protection.requiresStrictStatusChecks
    ):
        # this is the same logic as above
        if pull_request.mergeStateStatus == MergeStateStatus.BEHIND:
            raise NeedsBranchUpdate("behind branch. need update")

    if pull_request.mergeStateStatus == pull_request.mergeStateStatus.BLOCKED:
        # figure out why we can't merge. There isn't a way to get this simply from the Github API. We need to find out ourselves.
        #
        # I think it's possible to find out blockers from branch protection issues
        # https://developer.github.com/v4/object/branchprotectionrule/?#fields
        #
        # - missing reviews
        # - blocking reviews
        # - missing required status checks
        # - failing required status checks
        # - branch not up to date (should be handled before this)
        # - missing required signature
        if (
            branch_protection.requiresApprovingReviews
            and branch_protection.requiredApprovingReviewCount
        ):
            successful_reviews = 0
            for review in reviews:
                # blocking review
                if review.state == PRReviewState.CHANGES_REQUESTED:
                    raise NotQueueable("blocking review")
                # successful review
                if review.state == PRReviewState.APPROVED:
                    successful_reviews += 1
            # missing required review count
            if successful_reviews < branch_protection.requiredApprovingReviewCount:
                raise NotQueueable("missing required review count")

        if branch_protection.requiresStatusChecks:
            failing_contexts: typing.List[str] = []
            pending_contexts: typing.List[str] = []
            passing_contexts: typing.List[str] = []
            b_log = log.bind(
                contexts=contexts,
                required=branch_protection.requiredStatusCheckContexts,
            )
            required = set(branch_protection.requiredStatusCheckContexts)
            for status_context in contexts:
                if status_context.state in (StatusState.ERROR, StatusState.FAILURE):
                    failing_contexts.append(status_context.context)
                elif status_context.state in (
                    StatusState.EXPECTED,
                    StatusState.PENDING,
                ):
                    pending_contexts.append(status_context.context)
                else:
                    assert status_context.state == StatusState.SUCCESS
                    passing_contexts.append(status_context.context)

            failing = set(failing_contexts)
            # we have failing statuses that are required
            if len(required - failing) < len(required):
                # NOTE(chdsbd): We need to skip this PR because it would block
                # the merge queue. We may be able to bump it to the back of the
                # queue, but it's easier just to remove it all together. There
                # is a similar question for the review counting.
                raise NotQueueable("failing required status checks")
            passing = set(passing_contexts)
            if len(required - passing) > 0:
                raise WaitingForChecks("missing required status checks")

        if branch_protection.requiresCommitSignatures:
            if not valid_signature:
                raise NotQueueable("missing required signature")
        raise NotQueueable("Could not determine why PR is blocked")
    # okay to merge
    return None