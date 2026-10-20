"""The estimated shadow detail metric is explicitly labeled in the payload."""

from services.aml_builder.web.services.tools import _shadow_count_payload
from services.aml_builder.web.api.codec import ShadowPayload


def test_shadow_detail_count_remains_unchanged_and_is_marked_as_estimated() -> None:
    """P10 adds estimate metadata without changing the existing count values."""
    payload = _shadow_count_payload(7)
    assert payload == {
        "header_alert_count": 7,
        "transaction_detail_count": 28,
        "alert_density_ratio": 4.0,
        "detail_count_is_estimate": True,
        "detail_count_basis": "header_count*4",
    }
    assert ShadowPayload.model_validate(payload).detail_count_is_estimate is True

