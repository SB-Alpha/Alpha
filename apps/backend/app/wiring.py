"""Connects features that must not import each other."""

import logging

from pydantic import ValidationError

from app.features.db_migration.schemas import SourceInput, SourceLookup

logger = logging.getLogger(__name__)


def discover_source(lookup: SourceLookup) -> SourceInput:
    """Find the source with the assessment module, from the keys given for this run."""
    from app.features.assessment.schemas import AssessmentInput
    from app.features.assessment.service import run as assess

    found = assess(AssessmentInput(run_id=lookup.run_id, aws_credentials_ref=lookup.cloud_ref,
                                   db_admin_ref=lookup.db_admin_ref, aws_region=lookup.region,
                                   target_region=lookup.target_region))
    try:
        return SourceInput.model_validate(found.source.model_dump())
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise RuntimeError(f"source lookup is incomplete: {', '.join(fields)}; found problems: {found.gaps}") from None
