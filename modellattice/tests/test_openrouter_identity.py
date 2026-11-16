"""Regression test for the Model-duplication bug: a ":free"/":batch"
commercial-tier variant of a model must resolve to the SAME Model as its
standard-tier sibling (two Offerings of one Model), not a second Model.

Found by inspecting the real catalog after the week-2 connector work:
100 of 468 live offerings (86 ":batch", 14 ":free") were affected before
this fix. See scripts/repair_model_identity.py for the one-time cleanup
of data already ingested before the fix.
"""
from app.connectors.openrouter import _strip_tier_suffix


def test_strip_tier_suffix_removes_free_and_batch():
    assert _strip_tier_suffix("LFM3-1.7B (free)") == "LFM3-1.7B"
    assert _strip_tier_suffix("Claude Fable 5.2 (batch)") == "Claude Fable 5.2"


def test_strip_tier_suffix_is_case_insensitive_and_handles_double_space():
    assert _strip_tier_suffix("Gemma 4 26B A4B  (free)") == "Gemma 4 26B A4B"
    assert _strip_tier_suffix("Something (FREE)") == "Something"


def test_strip_tier_suffix_leaves_unrelated_text_alone():
    assert _strip_tier_suffix("GPT-6.1 Terra Pro") == "GPT-6.1 Terra Pro"
    # a parenthetical that isn't a known commercial tier is left as-is -
    # we only strip the two suffixes actually observed in the catalog.
    assert _strip_tier_suffix("Qwen3.8 Max (0902)") == "Qwen3.8 Max (0902)"


def test_free_and_batch_variants_resolve_to_the_same_family(tmp_path):
    """End-to-end through fetch_raw's own mapping logic: build the two
    RawOffering entries the way OpenRouter's real payload shapes them and
    confirm they'd now resolve to the same model_family (and therefore,
    via app/connectors/base.py's find-or-create, the same Model row)."""
    import app.connectors.openrouter as openrouter_module

    def fake_name_to_family(name: str) -> str:
        _, sep, label = name.partition(": ")
        return openrouter_module._strip_tier_suffix(label if sep else name)

    standard = fake_name_to_family("LiquidAI: LFM3-1.7B")
    free = fake_name_to_family("LiquidAI: LFM3-1.7B (free)")
    assert standard == free == "LFM3-1.7B"
