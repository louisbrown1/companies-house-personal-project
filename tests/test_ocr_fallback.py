from types import SimpleNamespace

import pytest
from PIL import Image

from agent_ui import ocr_fallback as ocr


@pytest.mark.parametrize(
    "original,reading,expected",
    [
        (
            "Support of £2Om was received.",
            "Support of E20m was received.",
            "Support of £20m was received.",
        ),
        (
            "The facility drew £8OOk today.",
            "The facility drew €800k today.",
            "The facility drew £800k today.",
        ),
        (
            "A payment of £O.4m was made.",
            "A payment of £0.4m was made.",
            "A payment of £0.4m was made.",
        ),
    ],
)
def test_currency_requires_two_source_readings(original, reading, expected):
    assert ocr.agreed_text(original, [(reading, 0.99)]) is None
    assert ocr.agreed_text(original, [(reading, 0.99)] * 2) == expected


@pytest.mark.parametrize(
    "reading",
    [
        "Support of £30m was received.",
        "Support of £20k was received.",
        "Support of £2.0m was received.",
        "Support of £20m was not received.",
    ],
)
def test_source_disagreement_cannot_change_value_units_or_meaning(reading):
    assert ocr.agreed_text("Support of £2Om was received.", [(reading, 0.99)] * 2) is None


def test_valid_amount_and_low_confidence_cannot_be_overridden():
    assert (
        ocr.agreed_text("Support of £20m was received.", [("Support of £30m was received.", 1)] * 2)
        is None
    )
    assert (
        ocr.agreed_text(
            "Support of £2Om was received.", [("Support of £20m was received.", 0.8)] * 2
        )
        is None
    )


def test_crop_fallback_preserves_geometry_provenance_and_engine_state(monkeypatch):
    monkeypatch.setattr(ocr, "windows_read", lambda images: [])

    class Engine:
        use_det = True
        use_cls = True
        use_rec = True

        def __call__(self, image, **kwargs):
            self.use_det = False
            self.use_cls = False
            return SimpleNamespace(txts=["A payment of £0.4m was made."], scores=[0.99])

    engine = Engine()
    geometry = [[0, 0], [100, 0], [100, 20], [0, 20]]
    page = {"boxes": [{"box": geometry, "text": "A payment of £O.4m was made.", "score": 0.99}]}
    result = ocr.refine_crops(page, Image.new("RGB", (250, 60)), engine)
    box = result["boxes"][0]
    assert box["text"] == "A payment of £0.4m was made."
    assert box["box"] == geometry
    assert box["fallback"]["original_text"] == "A payment of £O.4m was made."
    assert box["score"] == 0.99
    assert engine.use_det and engine.use_cls and engine.use_rec


@pytest.mark.parametrize("agree", [True, False])
def test_garbled_line_needs_independent_engine_agreement(monkeypatch, agree):
    good = "The assets remain on the company balance sheet."
    monkeypatch.setattr(
        ocr,
        "windows_read",
        lambda images: [
            [good if agree else "The assets were sold.", good if agree else "Different text."]
        ],
    )

    def engine(*args, **kwargs):
        return SimpleNamespace(txts=[good], scores=[0.99])

    page = {
        "boxes": [
            {"box": [[0, 0], [100, 0], [100, 20], [0, 20]], "text": "th a s s sh", "score": 0.5}
        ]
    }
    box = ocr.refine_crops(page, Image.new("RGB", (250, 60)), engine)["boxes"][0]
    assert box["text"] == (good if agree else "th a s s sh")
    assert bool(box.get("fallback")) == agree


def test_recorded_source_fallback_recovers_disclosures_without_changing_capital():
    import json
    from pathlib import Path

    from agent_ui.banking_notes import extract_banking_notes

    document = json.loads(
        (Path(__file__).parent / "fixtures/ocr_fallback_recovered.json").read_text(encoding="utf-8")
    )
    identity = document["identity"]
    notes = extract_banking_notes(document, identity["period"], identity["company_name"])
    text = "\n".join(d["excerpt"] for d in notes["disclosures"])
    for amount in ["£20m", "£800k", "£0.2m", "£0.4m", "£1,120.8m", "£8,374k"]:
        assert amount in text
    assert len(notes["disclosures"]) == 7
    assert not notes["retry_pages"]
    assert {m["value"] for m in notes["metrics"]} == {"193329", "171791"}
    repaired = [b for p in document["pages"] for b in p["boxes"] if b.get("fallback")]
    assert repaired
    assert all(len(b["fallback"]["readings"]) >= 2 for b in repaired)


def test_failed_crop_preserves_original_and_engine_state(monkeypatch):
    monkeypatch.setattr(ocr, "windows_read", lambda images: [])

    class Engine:
        use_det = True
        use_cls = True
        use_rec = True

        def __call__(self, *args, **kwargs):
            self.use_det = False
            raise RuntimeError("OCR unavailable")

    engine = Engine()
    page = {
        "boxes": [
            {
                "box": [[0, 0], [100, 0], [100, 20], [0, 20]],
                "text": "Uncertain source line",
                "score": 0.5,
            }
        ]
    }
    box = ocr.refine_crops(page, Image.new("RGB", (250, 60)), engine)["boxes"][0]
    assert box["text"] == "Uncertain source line"
    assert not box.get("fallback")
    assert engine.use_det
