#!/usr/bin/env python3
"""Offer comparison for the Veto MCP server.

For the "I have multiple offers, help me think clearly" part of the
loop. All money figures are **user-supplied** — this module never
invents market data. Anything derived (per-year equity vest, PTO
valuation) is labeled ``(estimated)`` in output.

``add_offer(name, base, bonus=0, equity_total=0, equity_years=4,
benefits_value=0, pto_days=15, remote="onsite", location="",
growth_score=5, notes="")``
    Validate and store an offer in ``offers.json`` (atomic writes).

``total_comp_4yr(offer)``
    Deterministic: 4 x (base + bonus) + equity_total + 4 x benefits_value.

``compare_offers(weights=None)``
    Ranked offers with a per-offer dimension breakdown. Default
    weights: compensation 40, growth 20, benefits 15, flexibility 15,
    location_fit 10. Scores are normalized 0-100 per dimension.

``set_weights({...})`` / ``get_weights()``
    Override the dimension weights (must sum to 100; partial overrides
    merge with the current weights). Persisted to
    ``offer_weights.json``.

``offer_report(offer_id)``
    Human-readable breakdown: the 4-yr comp math shown explicitly,
    dimension scores, and "what would need to be true for this offer to
    win" — the gap to #1 in plain language.

Stdlib only. Offers live in ``offers.json`` next to this module; tests
redirect the store paths to tmp dirs so they run offline.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.offer_compare")

BASE_DIR = Path(__file__).resolve().parent
OFFERS_PATH = BASE_DIR / "offers.json"
WEIGHTS_PATH = BASE_DIR / "offer_weights.json"

#: Flexibility dimension is a fixed 0-100 scale from the remote field.
REMOTE_FLEXIBILITY = {"onsite": 20.0, "hybrid": 60.0, "remote": 100.0}

#: Dimension key -> default weight. Weights must always sum to 100.
DEFAULT_WEIGHTS: dict[str, float] = {
    "compensation": 40.0,
    "growth": 20.0,
    "benefits": 15.0,
    "flexibility": 15.0,
    "location_fit": 10.0,
}
DIMENSIONS = list(DEFAULT_WEIGHTS)

#: Workdays per year — used ONLY for the labeled PTO valuation estimate.
WORKDAYS_PER_YEAR = 260

#: A location_fit of None means "no score given" -> neutral, non-differentiating.
LOCATION_NEUTRAL = 50.0


# ---------------------------------------------------------------------------
# Store helpers (atomic writes, corrupt-file tolerant)
# ---------------------------------------------------------------------------


def _load_json(path: Path, default: Any) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default
    return data


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp, path)


def _load_offers() -> list[dict[str, Any]]:
    data = _load_json(OFFERS_PATH, [])
    return data if isinstance(data, list) else []


def _save_offers(offers: list[dict[str, Any]]) -> None:
    _atomic_write(OFFERS_PATH, offers)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _num(value: Any, name: str, minimum: float = 0.0,
         maximum: float | None = None) -> float:
    """Coerce to a number and range-check. Rejects bools and non-numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    result = float(value)
    if result != result:  # NaN
        raise ValueError(f"{name} must be a number, got NaN")
    if result < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value!r}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got {value!r}")
    return result


def _validate_offer_fields(
    name: str,
    base: Any,
    bonus: Any,
    equity_total: Any,
    equity_years: Any,
    benefits_value: Any,
    pto_days: Any,
    remote: str,
    location: str,
    growth_score: Any,
    location_score: Any,
    notes: str,
) -> dict[str, Any]:
    name = (name or "").strip() if isinstance(name, str) else ""
    if not name:
        raise ValueError("name is required")
    base = _num(base, "base", minimum=0.0)
    remote_key = str(remote or "onsite").strip().lower()
    if remote_key not in REMOTE_FLEXIBILITY:
        raise ValueError(
            f"remote must be one of {sorted(REMOTE_FLEXIBILITY)}, "
            f"got {remote!r}"
        )
    loc_score: float | None = None
    if location_score is not None:
        loc_score = _num(location_score, "location_score", 0.0, 100.0)
    return {
        "name": name,
        "base": base,
        "bonus": _num(bonus, "bonus", 0.0),
        "equity_total": _num(equity_total, "equity_total", 0.0),
        "equity_years": _num(equity_years, "equity_years", minimum=1e-9),
        "benefits_value": _num(benefits_value, "benefits_value", 0.0),
        "pto_days": _num(pto_days, "pto_days", 0.0),
        "remote": remote_key,
        "location": str(location or ""),
        "growth_score": _num(growth_score, "growth_score", 1.0, 10.0),
        "location_score": loc_score,
        "notes": str(notes or ""),
    }


# ---------------------------------------------------------------------------
# Core: add / get / list
# ---------------------------------------------------------------------------


def add_offer(
    name: str,
    base: float,
    bonus: float = 0,
    equity_total: float = 0,
    equity_years: float = 4,
    benefits_value: float = 0,
    pto_days: float = 15,
    remote: str = "onsite",
    location: str = "",
    growth_score: float = 5,
    notes: str = "",
    location_score: float | None = None,
) -> dict[str, Any]:
    """Validate and store an offer. Returns {"ok": True, "offer": {...}}.

    Raises ValueError on invalid input. ``location_score`` (0-100) is an
    optional extra: when omitted the location_fit dimension is neutral
    (50) for that offer and does not differentiate.
    """
    offer = _validate_offer_fields(
        name, base, bonus, equity_total, equity_years, benefits_value,
        pto_days, remote, location, growth_score, location_score, notes,
    )
    offer["id"] = "offer-" + uuid.uuid4().hex[:8]
    offer["created_at"] = datetime.now(timezone.utc).isoformat()
    offers = _load_offers()
    offers.append(offer)
    _save_offers(offers)
    return {"ok": True, "offer": offer}


def get_offer(offer_id: str) -> dict[str, Any] | None:
    """Return the stored offer dict, or None."""
    for offer in _load_offers():
        if offer.get("id") == offer_id:
            return offer
    return None


def list_offers() -> dict[str, Any]:
    """Return all stored offers."""
    return {"ok": True, "offers": _load_offers()}


# ---------------------------------------------------------------------------
# Compensation math
# ---------------------------------------------------------------------------


def total_comp_4yr(offer: dict[str, Any]) -> float:
    """Deterministic 4-year total comp.

    4 x (base + bonus) + equity_total + 4 x benefits_value.
    All inputs are user-supplied; nothing is estimated here.
    """
    return (
        4.0 * (float(offer.get("base", 0)) + float(offer.get("bonus", 0)))
        + float(offer.get("equity_total", 0))
        + 4.0 * float(offer.get("benefits_value", 0))
    )


def pto_value_estimate(offer: dict[str, Any]) -> float:
    """Estimated annual dollar value of PTO: pto_days x (base / 260).

    This is a rough estimate, not a fact — every caller labels it as
    such. Uses base salary only (never invents a "loaded" rate).
    """
    return float(offer.get("pto_days", 0)) * float(offer.get("base", 0)) / WORKDAYS_PER_YEAR


def _fmt_money(value: float) -> str:
    return f"${value:,.0f}"


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------


def get_weights() -> dict[str, float]:
    """Current dimension weights (persisted, else defaults)."""
    stored = _load_json(WEIGHTS_PATH, None)
    if isinstance(stored, dict):
        cleaned = {
            k: float(v) for k, v in stored.items() if k in DEFAULT_WEIGHTS
        }
        if set(cleaned) == set(DEFAULT_WEIGHTS) and abs(
            sum(cleaned.values()) - 100.0
        ) < 1e-9:
            return cleaned
    return dict(DEFAULT_WEIGHTS)


def set_weights(overrides: dict[str, float]) -> dict[str, Any]:
    """Override dimension weights. Partial overrides merge with current.

    The resulting weights must sum to 100 (exactly). Raises ValueError
    on unknown dimensions, negative values, or a bad total.
    """
    if not isinstance(overrides, dict) or not overrides:
        raise ValueError("set_weights needs a non-empty mapping")
    merged = get_weights()
    for key, value in overrides.items():
        if key not in DEFAULT_WEIGHTS:
            raise ValueError(
                f"unknown dimension {key!r}; "
                f"known: {sorted(DEFAULT_WEIGHTS)}"
            )
        merged[key] = _num(value, f"weight[{key}]", minimum=0.0)
    total = sum(merged.values())
    if abs(total - 100.0) > 1e-9:
        raise ValueError(f"weights must sum to 100, got {total}")
    _atomic_write(WEIGHTS_PATH, merged)
    return {"ok": True, "weights": merged}


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _minmax(values: list[float]) -> list[float]:
    """Normalize a list to 0-100. No spread -> everyone gets 100."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo <= 1e-9:
        return [100.0] * len(values)
    return [100.0 * (v - lo) / (hi - lo) for v in values]


def compare_offers(
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Rank stored offers by weighted dimension scores.

    Returns {"ok", "weights", "offers": [ranked entries], "markdown"}.
    Each entry: rank, offer, comp_4yr, dimensions {5 x 0-100}, and
    weighted_total. Scores normalize 0-100 per dimension:
    compensation/benefits use min-max across the offers on file, growth
    is growth_score/10, flexibility is a fixed onsite/hybrid/remote
    scale, location_fit is the user's location_score (or neutral 50).
    """
    offers = _load_offers()
    if not offers:
        return {"ok": False, "error": "no offers stored yet — add one with add_offer"}

    active = get_weights() if weights is None else dict(weights)
    if set(active) != set(DEFAULT_WEIGHTS) or abs(sum(active.values()) - 100.0) > 1e-9:
        return {
            "ok": False,
            "error": "weights must cover all 5 dimensions and sum to 100",
        }

    comps = [total_comp_4yr(o) for o in offers]
    benefits_raw = [
        float(o.get("benefits_value", 0)) + pto_value_estimate(o)
        for o in offers
    ]
    comp_scores = _minmax(comps)
    benefit_scores = _minmax(benefits_raw)

    entries: list[dict[str, Any]] = []
    for offer, comp, comp_s, ben_s in zip(offers, comps, comp_scores, benefit_scores):
        dims = {
            "compensation": round(comp_s, 1),
            "growth": round(float(offer["growth_score"]) / 10.0 * 100.0, 1),
            "benefits": round(ben_s, 1),
            "flexibility": round(REMOTE_FLEXIBILITY[offer["remote"]], 1),
            "location_fit": round(
                float(offer["location_score"])
                if offer.get("location_score") is not None
                else LOCATION_NEUTRAL,
                1,
            ),
        }
        total = round(
            sum(active[d] / 100.0 * dims[d] for d in DIMENSIONS), 1
        )
        entries.append(
            {
                "offer": offer,
                "comp_4yr": round(comp, 2),
                "dimensions": dims,
                "weighted_total": total,
            }
        )

    entries.sort(key=lambda e: (-e["weighted_total"], e["offer"]["name"]))
    for i, entry in enumerate(entries, 1):
        entry["rank"] = i

    result: dict[str, Any] = {
        "ok": True,
        "weights": active,
        "offers": entries,
    }
    result["markdown"] = _compare_markdown(result)
    return result


def _compare_markdown(result: dict[str, Any]) -> str:
    lines = ["# Offer comparison", ""]
    weights = result["weights"]
    lines.append(
        "Weights: "
        + ", ".join(f"{d} {weights[d]:g}" for d in DIMENSIONS)
    )
    lines.append("")
    for entry in result["offers"]:
        offer = entry["offer"]
        dims = entry["dimensions"]
        lines.append(
            f"## #{entry['rank']} {offer['name']} — "
            f"{entry['weighted_total']:.1f} pts"
        )
        lines.append(f"- 4-yr total comp: {_fmt_money(entry['comp_4yr'])}")
        lines.append(
            "- Dimensions: "
            + ", ".join(f"{d} {dims[d]:.0f}" for d in DIMENSIONS)
        )
        remote = offer["remote"]
        loc = offer.get("location") or "location not given"
        lines.append(
            f"- {remote}, {loc}, growth {offer['growth_score']:g}/10"
        )
        lines.append("")
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# Report: breakdown + "what would need to be true to win"
# ---------------------------------------------------------------------------


def _win_conditions(
    target: dict[str, Any],
    ranked: list[dict[str, Any]],
    weights: dict[str, float],
    comp_spread: float,
    benefits_spread: float,
) -> list[str]:
    """Plain-language paths for ``target`` to tie the current leader.

    Each path holds every other dimension fixed and translates the
    needed raw-dimension gain into real terms (dollars, score points).
    """
    leader = ranked[0]
    gap = leader["weighted_total"] - target["weighted_total"]
    offer = target["offer"]
    name = offer["name"]
    if gap <= 0.05:
        lines = [
            f"'{name}' currently leads the ranking at "
            f"{target['weighted_total']:.1f} pts.",
        ]
        best = max(target["dimensions"], key=lambda d: target["dimensions"][d])
        lines.append(
            f"Its strongest dimension is {best} "
            f"({target['dimensions'][best]:.0f}/100)."
        )
        return lines

    lines = [
        f"'{name}' is {gap:.1f} weighted points behind "
        f"'{leader['offer']['name']}' (rank #1).",
        "What would need to be true for it to win — single-dimension "
        "paths, holding everything else fixed:",
    ]
    dims = target["dimensions"]
    for dim in DIMENSIONS:
        weight = weights[dim]
        if weight <= 0:
            # A zero-weighted dimension cannot move the total — no path.
            continue
        need = gap * 100.0 / weight  # raw dimension points needed
        current = dims[dim]
        headroom = 100.0 - current
        label = dim.replace("_", " ")

        if dim == "compensation":
            if comp_spread <= 1e-9:
                text = ("compensation is currently tied across offers — any "
                        "raise re-sets the scale, so add ~$X and re-run")
            else:
                dollars = need * comp_spread / 100.0
                if need > headroom + 1e-9:
                    text = (f"not winnable on {label} alone "
                            f"(needs +{need:.0f} pts, only {headroom:.0f} available)")
                else:
                    text = (f"+{_fmt_money(dollars)} more in 4-yr total comp "
                            f"(about +{_fmt_money(dollars / 4)}/yr)")
            lines.append(f"- {label}: {text}.")
        elif dim == "growth":
            new_score = offer["growth_score"] + need / 10.0
            if new_score > 10.0 + 1e-9:
                text = (f"not winnable on {label} alone "
                        f"(would need {new_score:.1f}/10)")
            else:
                text = (f"raise growth score from {offer['growth_score']:g} "
                        f"to {new_score:.1f}/10")
            lines.append(f"- {label}: {text}.")
        elif dim == "benefits":
            if benefits_spread <= 1e-9:
                text = ("benefits are currently tied across offers — any "
                        "improvement re-sets the scale")
            else:
                dollars = need * benefits_spread / 100.0
                if need > headroom + 1e-9:
                    text = (f"not winnable on {label} alone "
                            f"(needs +{need:.0f} pts, only {headroom:.0f} available)")
                else:
                    text = (f"+{_fmt_money(dollars)} more in annual benefits "
                            f"value (or equivalent PTO)")
            lines.append(f"- {label}: {text}.")
        elif dim == "flexibility":
            remote = offer["remote"]
            order = ["onsite", "hybrid", "remote"]
            idx = order.index(remote)
            if idx == len(order) - 1:
                lines.append(f"- {label}: already maxed (fully remote).")
                continue
            nxt = order[idx + 1]
            gain = (REMOTE_FLEXIBILITY[nxt] - REMOTE_FLEXIBILITY[remote]) * weight / 100.0
            if gain >= gap - 1e-9:
                text = (f"moving to {nxt} adds {gain:.1f} weighted points — "
                        "enough to take the lead")
            else:
                text = (f"moving to {nxt} adds {gain:.1f} weighted points "
                        f"(still {gap - gain:.1f} short)")
            lines.append(f"- {label}: {text}.")
        elif dim == "location_fit":
            if offer.get("location_score") is None:
                lines.append(
                    f"- {label}: no location score set for this offer "
                    f"(currently neutral {LOCATION_NEUTRAL:.0f}) — score it "
                    "0-100 to make this dimension meaningful."
                )
            elif need > headroom + 1e-9:
                lines.append(
                    f"- {label}: not winnable on {label} alone "
                    f"(needs +{need:.0f} pts, only {headroom:.0f} available)."
                )
            else:
                lines.append(
                    f"- {label}: raise location fit from {current:.0f} "
                    f"to {current + need:.0f}/100."
                )
    lines.append(
        "In practice a mix works — these are separate levers, not a plan."
    )
    return lines


def offer_report(offer_id: str) -> dict[str, Any]:
    """Human-readable breakdown for one offer.

    Shows the 4-yr comp math explicitly, dimension scores, and what
    would need to be true for this offer to win. Derived figures are
    labeled (estimated); every other number is user-supplied.
    """
    offer = get_offer(offer_id)
    if offer is None:
        return {"ok": False, "error": f"unknown offer id {offer_id!r}"}

    comparison = compare_offers()
    if not comparison.get("ok"):
        return comparison
    ranked = comparison["offers"]
    weights = comparison["weights"]
    entry = next(e for e in ranked if e["offer"]["id"] == offer_id)

    comp = total_comp_4yr(offer)
    pto_est = pto_value_estimate(offer)
    annual_equity = offer["equity_total"] / offer["equity_years"]
    dims = entry["dimensions"]

    comps = [total_comp_4yr(e["offer"]) for e in ranked]
    comp_spread = max(comps) - min(comps)
    bens = [
        float(e["offer"].get("benefits_value", 0)) + pto_value_estimate(e["offer"])
        for e in ranked
    ]
    benefits_spread = max(bens) - min(bens)

    lines = [f"# Offer report: {offer['name']}", ""]
    lines.append(f"Rank #{entry['rank']} of {len(ranked)} "
                 f"({entry['weighted_total']:.1f} weighted pts)")
    lines.append("")
    lines.append("## 4-year compensation (your numbers)")
    lines.append(
        f"4 x ({_fmt_money(offer['base'])} base + {_fmt_money(offer['bonus'])} bonus) "
        f"+ {_fmt_money(offer['equity_total'])} equity "
        f"+ 4 x {_fmt_money(offer['benefits_value'])} benefits/year"
    )
    lines.append(f"= **{_fmt_money(comp)}** over 4 years")
    lines.append(
        f"- Equity vests over {offer['equity_years']:g} years "
        f"(about {_fmt_money(annual_equity)}/yr, estimated)"
    )
    lines.append(
        f"- PTO value: {offer['pto_days']:g} days x "
        f"({_fmt_money(offer['base'])}/260) = about {_fmt_money(pto_est)}/yr (estimated)"
    )
    if offer.get("location"):
        lines.append(f"- Location: {offer['location']} ({offer['remote']})")
    else:
        lines.append(f"- Work mode: {offer['remote']}")
    lines.append(f"- Growth score (your rating): {offer['growth_score']:g}/10")
    if offer.get("notes"):
        lines.append(f"- Notes: {offer['notes']}")
    lines.append("")
    lines.append("## Dimension scores (0-100, normalized across your offers)")
    for dim in DIMENSIONS:
        lines.append(f"- {dim.replace('_', ' ')}: {dims[dim]:.1f} "
                     f"(weight {weights[dim]:g})")
    lines.append("")
    lines.append("## What would need to be true for this offer to win")
    lines.extend(_win_conditions(entry, ranked, weights, comp_spread, benefits_spread))
    lines.append("")
    lines.append(
        "_Every number above came from you. Figures marked (estimated) are "
        "rough derivations, not market data._"
    )

    return {
        "ok": True,
        "offer": offer,
        "rank": entry["rank"],
        "of": len(ranked),
        "comp_4yr": comp,
        "pto_value_estimate": pto_est,
        "dimensions": dims,
        "weighted_total": entry["weighted_total"],
        "weights": weights,
        "markdown": "\n".join(lines).strip() + "\n",
    }


# ---------------------------------------------------------------------------
# Wiring: MCP tools + CLI (server.py / cli.py call these; this module is
# never imported by them at module load, so there is no import cycle)
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register the offer-comparison MCP tools on an MCP server instance."""
    # Bind module-level implementations explicitly: the @mcp.tool()
    # wrappers reuse the public names, which would otherwise shadow the
    # globals inside this scope.
    _impl_add = globals()["add_offer"]
    _impl_list = globals()["list_offers"]
    _impl_compare = globals()["compare_offers"]
    _impl_report = globals()["offer_report"]
    _impl_set_w = globals()["set_weights"]
    _impl_get_w = globals()["get_weights"]

    @mcp.tool()
    def add_offer(
        name: str,
        base: float,
        bonus: float = 0,
        equity_total: float = 0,
        equity_years: float = 4,
        benefits_value: float = 0,
        pto_days: float = 15,
        remote: str = "onsite",
        location: str = "",
        growth_score: float = 5,
        notes: str = "",
        location_score: float | None = None,
    ) -> dict:
        """Store a job offer for comparison.

        Args:
            name: Offer label, e.g. "Acme".
            base: Annual base salary (your number).
            bonus: Annual target bonus.
            equity_total: Total equity value over the vesting period.
            equity_years: Vesting period in years.
            benefits_value: Annual dollar value of benefits (your number).
            pto_days: Paid time off days per year.
            remote: "onsite", "hybrid", or "remote".
            location: City/region string.
            growth_score: Your 1-10 rating of role/career growth.
            notes: Free-text notes.
            location_score: Optional 0-100 location-fit score; when
                omitted the location dimension stays neutral.

        Returns:
            Dict with ok and the stored offer (including its id).
        """
        return _impl_add(
            name, base, bonus=bonus, equity_total=equity_total,
            equity_years=equity_years, benefits_value=benefits_value,
            pto_days=pto_days, remote=remote, location=location,
            growth_score=growth_score, notes=notes,
            location_score=location_score,
        )

    @mcp.tool()
    def list_offers() -> dict:
        """List all stored offers.

        Returns:
            Dict with ok and the offers list.
        """
        return _impl_list()

    @mcp.tool()
    def compare_offers() -> dict:
        """Rank stored offers by weighted dimension scores.

        Dimensions: compensation (40), growth (20), benefits (15),
        flexibility (15), location_fit (10) — each normalized 0-100.
        Override with set_offer_weights.

        Returns:
            Dict with ok, weights, ranked offers (each with dimension
            scores and weighted_total), and markdown.
        """
        return _impl_compare()

    @mcp.tool()
    def offer_report(offer_id: str) -> dict:
        """Full breakdown for one offer.

        Args:
            offer_id: The offer id from add_offer/list_offers.

        Returns:
            Dict with explicit 4-yr comp math, dimension scores, and
            "what would need to be true for this offer to win" — the gap
            to #1 in plain language. Derived figures are labeled
            (estimated); all other numbers are user-supplied.
        """
        return _impl_report(offer_id)

    @mcp.tool()
    def set_offer_weights(weights: dict) -> dict:
        """Override comparison dimension weights (must sum to 100).

        Args:
            weights: Mapping like {"compensation": 45, "growth": 15}.
                Partial overrides merge with the current weights.

        Returns:
            Dict with ok and the resulting weights.
        """
        return _impl_set_w(weights)

    @mcp.tool()
    def get_offer_weights() -> dict:
        """Return the current comparison dimension weights."""
        return {"ok": True, "weights": _impl_get_w()}


def _print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result.get("markdown") or json.dumps(result, indent=2))


def cmd_offers(args: Any) -> int:
    """CLI handler for `offers`."""
    as_json = getattr(args, "json", False)
    action = getattr(args, "action", "list")
    try:
        if action == "add":
            result = add_offer(
                args.name,
                args.base,
                bonus=args.bonus,
                equity_total=args.equity_total,
                equity_years=args.equity_years,
                benefits_value=args.benefits_value,
                pto_days=args.pto_days,
                remote=args.remote,
                location=args.location or "",
                growth_score=args.growth_score,
                notes=args.notes or "",
                location_score=args.location_score,
            )
            if as_json:
                print(json.dumps(result, indent=2))
            else:
                offer = result["offer"]
                print(f"Stored {offer['id']}: {offer['name']} "
                      f"({_fmt_money(total_comp_4yr(offer))} over 4 yrs)")
            return 0
        if action == "list":
            result = list_offers()
            if as_json:
                print(json.dumps(result, indent=2))
            else:
                offers = result["offers"]
                if not offers:
                    print("No offers stored yet.")
                for offer in offers:
                    print(f"{offer['id']}: {offer['name']} — "
                          f"{_fmt_money(total_comp_4yr(offer))} over 4 yrs")
            return 0
        if action == "compare":
            _print_result(compare_offers(), as_json)
            return 0
        if action == "report":
            _print_result(offer_report(args.offer_id), as_json)
            return 0
        if action == "weights":
            if getattr(args, "set", None):
                overrides: dict[str, float] = {}
                for pair in args.set:
                    key, _, val = pair.partition("=")
                    if not key or not val:
                        raise ValueError(
                            f"bad --set value {pair!r}; use key=value"
                        )
                    overrides[key.strip()] = float(val)
                result = set_weights(overrides)
            else:
                result = {"ok": True, "weights": get_weights()}
            if as_json:
                print(json.dumps(result, indent=2))
            else:
                print(", ".join(
                    f"{k} {v:g}" for k, v in result["weights"].items()
                ))
            return 0
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    print(f"error: unknown action {action!r}")
    return 1


def register_cli(sub: Any) -> dict[str, Any]:
    """Add `offers` to an argparse subparsers.

    Returns a {command: handler} mapping the caller can merge into its
    own dispatch table (cli.py-style).
    """
    parser = sub.add_parser(
        "offers", help="Compare job offers side by side."
    )
    parser.add_argument(
        "action",
        choices=["add", "list", "compare", "report", "weights"],
        help="What to do.",
    )
    parser.add_argument("--name", help="Offer label, e.g. 'Acme'.")
    parser.add_argument("--base", type=float, help="Annual base salary.")
    parser.add_argument("--bonus", type=float, default=0.0,
                        help="Annual target bonus.")
    parser.add_argument("--equity-total", type=float, default=0.0,
                        dest="equity_total",
                        help="Total equity value over the vest period.")
    parser.add_argument("--equity-years", type=float, default=4.0,
                        dest="equity_years", help="Vesting period in years.")
    parser.add_argument("--benefits-value", type=float, default=0.0,
                        dest="benefits_value",
                        help="Annual dollar value of benefits.")
    parser.add_argument("--pto-days", type=float, default=15.0,
                        dest="pto_days", help="PTO days per year.")
    parser.add_argument("--remote", default="onsite",
                        choices=["onsite", "hybrid", "remote"])
    parser.add_argument("--location", default="",
                        help="City/region string.")
    parser.add_argument("--growth-score", type=float, default=5.0,
                        dest="growth_score",
                        help="Your 1-10 rating of role/career growth.")
    parser.add_argument("--location-score", type=float, default=None,
                        dest="location_score",
                        help="Optional 0-100 location-fit score.")
    parser.add_argument("--notes", default="", help="Free-text notes.")
    parser.add_argument("--offer-id", default="",
                        help="Offer id for the report action.")
    parser.add_argument("--set", action="append", default=[],
                        help="Weight override as key=value; repeatable.")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable JSON output.")

    return {"offers": cmd_offers}
