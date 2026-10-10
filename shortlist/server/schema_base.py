"""The base every API model inherits.

There is exactly one rule here, and it is why this module exists rather than each router declaring
its own config: **a response model must document a payload, never filter it.**

A *default* Pydantic response model silently DROPS any key it does not declare. Not with an error —
the field simply stops arriving, in production only, blanking out part of a page while every test
that checks the fields the model DOES declare still passes. Across ~65 endpoints whose exact dict
shape the SPA already reads, one forgotten field is one broken screen.

``extra="allow"`` makes that impossible: undeclared keys pass through untouched. So a model can only
ever add schema documentation, and the cost of forgetting a field is a slightly thinner OpenAPI
schema rather than missing data.

Never declare a response model on a bare ``BaseModel``. `tests/unit/test_response_models.py` fails
if one appears, because this is not a rule to rely on remembering.

Request models are the opposite case and must NOT inherit this — rejecting an unknown field on the
way IN is how a typo in a client becomes a 422 instead of a silently ignored setting. A plain
``BaseModel`` does not do that either (Pydantic ignores unknown fields by default), so a request body
inherits :class:`StrictRequestModel`. The row body (`CollectionIn`) does; older request bodies still
sit on a bare ``BaseModel`` and ignore what they do not declare.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PassthroughModel(BaseModel):
    """Base for every response model: documents the payload without filtering it (see module doc)."""

    model_config = ConfigDict(extra="allow")


class StrictRequestModel(BaseModel):
    """Base for a request body: an unknown field is a 422, not a setting that silently never took."""

    model_config = ConfigDict(extra="forbid")
