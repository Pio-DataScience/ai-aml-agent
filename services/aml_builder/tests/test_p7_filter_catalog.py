"""P7 filter catalog validation tests."""

import pytest

from services.aml_builder.web.services.config_loader import (
    FilterDimension,
    load_catalogs,
)
from services.aml_builder.web.services.settings import settings


def test_repository_filter_catalog_has_enabled_code_resolver_and_disabled_advisor_rows():
    catalog = load_catalogs(settings.CATALOG_DIR).filters

    assert (
        catalog.dimensions["explanation_code"].value_source
        == "existing_explanation_code_catalog"
    )
    assert catalog.dimensions["explanation_code"].resolver_config.max_options > 0
    assert catalog.dimensions["account_status"].status == "needs_advisor_input"
    assert catalog.dimensions["customer_profession"].status == "needs_advisor_input"


def test_enabled_dimensions_require_resolver_contract_and_non_tbd_target():
    with pytest.raises(ValueError, match="target table and column"):
        FilterDimension.model_validate(
            {
                "label": "Bad",
                "level": "transaction",
                "target": {"table": "TBD", "column": "TBD"},
                "resolver": "semantic",
                "value_source": "x",
                "match": "any_of",
                "polarity_allowed": False,
                "required": False,
                "resolver_config": {
                    "match_threshold": 0.5,
                    "ambiguity_margin": 0.05,
                    "max_options": 5,
                },
                "status": "confirmed",
            }
        )
    with pytest.raises(ValueError, match="resolver_config"):
        FilterDimension.model_validate(
            {
                "label": "Bad",
                "level": "transaction",
                "target": {"table": "TX", "column": "CODE"},
                "resolver": "semantic",
                "value_source": "x",
                "match": "any_of",
                "polarity_allowed": False,
                "required": False,
                "status": "confirmed",
            }
        )
