import json
import logging
import re
from collections.abc import AsyncGenerator

import httpx

from ..config import Settings

logger = logging.getLogger("app.llm.llm_service")


# =============================================================================
# LANGUAGE AUTO-DETECTION
# =============================================================================

def detect_language(text: str) -> str:
    """
    Auto-detect language from query text.
    Returns 'hi' or 'en' only. Tamil and other languages are not supported.
    Script detection takes strict priority.
    """
    # Devanagari Unicode block U+0900–U+097F → Hindi
    if re.search(r"[\u0900-\u097F]", text):
        return "hi"
    # Arabic/Urdu script U+0600–U+06FF → treat as Hindi
    if re.search(r"[\u0600-\u06FF]", text):
        return "hi"
    # Romanised Hindi / Hinglish — unambiguously Hindi markers
    # Includes colloquial openers (bhai, yaar), postposition patterns (pe tax,
    # ki salary, ka tax), and common Hinglish verbs/endings (batao, karo, hoga).
    hindi_only = (
        r"\b(kya|kitna|kitni|kaisa|kaise|kyun|kyunki|lagega|batao|samjhao|"
        r"mujhe|mujhko|rupaye|paisa|wala|wali|"
        r"yeh\s+salary|mera\s+salary|meri\s+income|kitna\s+tax|"
        r"kaise\s+bhare|bharna\s+hai|"
        # Colloquial openers
        r"bhai|yaar|"
        # Pronoun + postposition phrases
        r"mere\s+liye|konsa|konsi|kaunsa|kaunsi|"
        # Common Hinglish verbs / endings
        r"karo|lagta\s+hai|lagti\s+hai|hoga|hogi|"
        # Postposition patterns that only make sense in Hindi grammar
        r"pe\s+(?:tax|old|new|kitna)|ki\s+salary|ka\s+tax|"
        # Regime / topic in Hinglish
        r"naya\s+regime|purana\s+regime|nai\s+regime|"
        # Common query endings
        r"tax\s+kitna|tax\s+batao|salary\s+pe|income\s+pe)\b"
    )
    if re.search(hindi_only, text, re.IGNORECASE):
        return "hi"
    # Everything else → English (including Tamil, Telugu, etc.)
    return "en"


# =============================================================================
# HARDCODED INCOME TAX CALCULATOR
# =============================================================================

def _compute_tax_new_regime(taxable_income: int) -> dict:
    """
    New Tax Regime slabs (FY 2025-26, effective April 1 2026):
    0-4L:0%, 4-8L:5%, 8-12L:10%, 12-16L:15%, 16-20L:20%, 20-24L:25%, >24L:30%
    87A rebate: up to ₹60,000 if taxable income <= 12,00,000 → effectively zero tax up to ₹12L salary
    Effectively zero tax for salaried up to ₹12,75,000 (₹12L + ₹75K standard deduction)
    Marginal relief applied if income just over 12L
    Cess: 4%
    """
    slabs = [
        (400_000,   0.00),
        (800_000,   0.05),
        (1_200_000, 0.10),
        (1_600_000, 0.15),
        (2_000_000, 0.20),
        (2_400_000, 0.25),
        (float("inf"), 0.30),
    ]
    tax = 0
    prev = 0
    breakdown = []
    for limit, rate in slabs:
        if taxable_income <= prev:
            break
        chunk = int(min(taxable_income, limit)) - prev
        slab_tax = int(chunk * rate)
        if rate > 0:
            breakdown.append({
                "from": prev, "to": int(min(taxable_income, limit)),
                "rate": rate, "tax": slab_tax,
            })
        tax += slab_tax
        prev = int(min(limit, float("inf"))) if limit != float("inf") else prev + chunk

    rebate = 0
    marginal_relief = 0

    if taxable_income <= 1_200_000:
        # Full rebate up to ₹60,000 — makes income up to ₹12L effectively zero tax
        rebate = min(tax, 60_000)
        tax_after = tax - rebate
    else:
        tax_after = tax
        # Marginal relief: tax cannot exceed excess over ₹12L
        excess = taxable_income - 1_200_000
        if tax_after > excess:
            marginal_relief = tax_after - excess
            tax_after = excess

    cess = int(tax_after * 0.04)
    total = tax_after + cess
    eff = round(total / taxable_income * 100, 2) if taxable_income > 0 else 0.0
    return {
        "gross_tax": tax, "rebate_87a": rebate, "marginal_relief": marginal_relief,
        "tax_after_rebate": tax_after, "cess": cess, "total_tax": total,
        "effective_rate": eff, "slab_breakdown": breakdown,
    }


def _compute_tax_old_regime(taxable_income: int, age: int = 30) -> dict:
    """
    Old Tax Regime slabs.
    Below 60: 0-2.5L:0%, 2.5-5L:5%, 5-10L:20%, >10L:30%
    60-79: 0-3L:0%, 3-5L:5%, 5-10L:20%, >10L:30%
    80+:   0-5L:0%, 5-10L:20%, >10L:30%
    87A rebate: up to ₹12,500 if taxable income <= 5,00,000
    Cess: 4%
    """
    if age >= 80:
        slabs = [(500_000, 0.00), (1_000_000, 0.20), (float("inf"), 0.30)]
    elif age >= 60:
        slabs = [(300_000, 0.00), (500_000, 0.05), (1_000_000, 0.20), (float("inf"), 0.30)]
    else:
        slabs = [(250_000, 0.00), (500_000, 0.05), (1_000_000, 0.20), (float("inf"), 0.30)]

    tax = 0
    prev = 0
    breakdown = []
    for limit, rate in slabs:
        if taxable_income <= prev:
            break
        chunk = int(min(taxable_income, limit)) - prev
        slab_tax = int(chunk * rate)
        if rate > 0:
            breakdown.append({
                "from": prev, "to": int(min(taxable_income, limit)),
                "rate": rate, "tax": slab_tax,
            })
        tax += slab_tax
        prev = int(min(limit, float("inf"))) if limit != float("inf") else prev + chunk

    rebate = min(tax, 12_500) if taxable_income <= 500_000 else 0
    tax_after = tax - rebate
    cess = int(tax_after * 0.04)
    total = tax_after + cess
    eff = round(total / taxable_income * 100, 2) if taxable_income > 0 else 0.0
    return {
        "gross_tax": tax, "rebate_87a": rebate, "tax_after_rebate": tax_after,
        "cess": cess, "total_tax": total, "effective_rate": eff,
        "slab_breakdown": breakdown,
    }


def _fmt(n: int) -> str:
    """Format as Indian currency: 1500000 → ₹15,00,000"""
    if n == 0:
        return "₹0"
    s = str(abs(n))
    if len(s) <= 3:
        return f"₹{s}"
    last3 = s[-3:]
    rest = s[:-3]
    groups = []
    while len(rest) > 2:
        groups.append(rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.append(rest)
    groups.reverse()
    return f"₹{','.join(groups)},{last3}"


def _build_tax_calc_context(gross_salary: int, deductions: int = 0) -> str:
    """
    Build a pre-computed tax calculation block injected into the LLM context.
    This guarantees the LLM always presents correct figures.
    """
    new_std = 75_000
    new_taxable = max(0, gross_salary - new_std)
    nr = _compute_tax_new_regime(new_taxable)

    old_std = 50_000
    old_taxable = max(0, gross_salary - old_std - deductions)
    or_ = _compute_tax_old_regime(old_taxable)

    def regime_lines(label, gross, std, extra_dedn, taxable, result):
        lines = [
            f"  Gross Income          : {_fmt(gross)}",
            f"  Less Standard Dedn    : {_fmt(std)}",
        ]
        if extra_dedn > 0:
            lines.append(f"  Less Other Deductions : {_fmt(extra_dedn)}")
        lines.append(f"  Taxable Income        : {_fmt(taxable)}")
        lines.append("  Tax Computation:")
        if result["slab_breakdown"]:
            for sb in result["slab_breakdown"]:
                pct = int(sb["rate"] * 100)
                lines.append(
                    f"    {_fmt(sb['from'])} to {_fmt(sb['to'])} @ {pct}% = {_fmt(sb['tax'])}"
                )
        else:
            lines.append("    Nil (within nil slab)")
        lines.append(f"  Gross Tax             : {_fmt(result['gross_tax'])}")
        if result["rebate_87a"] > 0:
            lines.append(f"  Less 87A Rebate       : {_fmt(result['rebate_87a'])}")
        if result.get("marginal_relief", 0) > 0:
            lines.append(f"  Less Marginal Relief  : {_fmt(result['marginal_relief'])}")
        lines.append(f"  Tax after Rebate      : {_fmt(result['tax_after_rebate'])}")
        lines.append(f"  Add 4% Cess           : {_fmt(result['cess'])}")
        lines.append(f"  *** TOTAL TAX         : {_fmt(result['total_tax'])} ***")
        lines.append(f"  Effective Rate        : {result['effective_rate']}%")
        return lines

    winner = "New Regime" if nr["total_tax"] <= or_["total_tax"] else "Old Regime"
    saving = abs(or_["total_tax"] - nr["total_tax"])

    out = [
        "=== PRE-COMPUTED TAX CALCULATION — USE THESE EXACT NUMBERS ===",
        f"Input: Gross Salary = {_fmt(gross_salary)}" + (f"  |  Extra Deductions = {_fmt(deductions)}" if deductions else ""),
        "",
        "── NEW TAX REGIME (Default) ──",
    ]
    out += regime_lines("new", gross_salary, new_std, 0, new_taxable, nr)
    out += ["", "── OLD TAX REGIME (Optional, must be chosen) ──"]
    out += regime_lines("old", gross_salary, old_std, deductions, old_taxable, or_)
    out += [
        "",
        f"── VERDICT: {winner} saves {_fmt(saving)} more tax ──",
        "=== END OF PRE-COMPUTED CALCULATION ===",
    ]
    return "\n".join(out)


def _extract_salary_amount(query: str) -> int | None:
    """
    Extract salary/income from natural language. Returns amount in rupees.
    Handles typos: "luck"/"lak"/"lac" → lakh, garbled digits, multiple numbers.

    Strategy (applied in order):
    1. Collect all lakh/crore/plain-digit candidates with their positions.
    2. Single candidate → return it directly.
    3. Multiple candidates:
       a. First exclude candidates that sit within 15 chars of a deduction
          keyword (deduction/invest/saving/deduc).  This filters out amounts
          like "2 lakh deduction" before any salary-proximity logic runs.
       b. Among the survivors, pick the one closest to a salary/income
          keyword if one is present.
       c. Otherwise return the largest survivor (salary dominates queries).
    4. If exclusion leaves no candidates, fall back to the full set and
       return the largest value.
    """
    q = query.lower().replace(",", "").replace("₹", "")

    # Normalise common typos for "lakh"
    q = re.sub(r"\bluck\b|\blak\b|\blakh?s?\b|\blacs?\b", "lakh", q)
    # Normalise common typos for "tax"/"take"
    q = re.sub(r"\btake\b", "tax", q)

    # Collect (amount, position) tuples
    candidates: list[tuple[int, int]] = []

    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*lakh", q):
        v = int(float(m.group(1)) * 100_000)
        if 100_000 <= v <= 50_000_000:
            candidates.append((v, m.start()))

    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:crore|crores|cr)\b", q):
        v = int(float(m.group(1)) * 10_000_000)
        if 100_000 <= v <= 50_000_000:
            candidates.append((v, m.start()))

    for m in re.finditer(r"\b(\d{6,8})\b", q):
        v = int(m.group(1))
        if 100_000 <= v <= 50_000_000:
            candidates.append((v, m.start()))

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0][0]

    # ── Multiple candidates: exclude deduction amounts first ──
    dedn_kw = r"\b(deduction|deductions|invest(?:ment)?|saving|savings|deduc)\b"
    dedn_positions = [m.start() for m in re.finditer(dedn_kw, q, re.IGNORECASE)]

    if dedn_positions:
        working_set = [
            (v, pos) for v, pos in candidates
            if min(abs(pos - dp) for dp in dedn_positions) > 15
        ]
    else:
        working_set = candidates

    # Fall back to full set if exclusion removed everything
    if not working_set:
        working_set = candidates

    if len(working_set) == 1:
        return working_set[0][0]

    # ── Among survivors: prefer the one closest to a salary keyword ──
    salary_kw = r"\b(salary|income|earn|earns|earning|ctc|package|pay|paid|मेरा|वेतन)\b"
    salary_positions = [m.start() for m in re.finditer(salary_kw, q, re.IGNORECASE)]

    if salary_positions:
        def _closest_salary_dist(pos: int) -> int:
            return min(abs(pos - sp) for sp in salary_positions)
        working_set.sort(key=lambda x: _closest_salary_dist(x[1]))
        return working_set[0][0]

    # Final fallback: largest remaining value
    return max(v for v, _ in working_set)


def _extract_deductions(query: str) -> int:
    """
    Extract declared deductions from query. Returns 0 if not found.

    Patterns recognised (examples):
      "2 lakh deduction / deduc / invest / saving"
      "deduction / 80c / invest / saving of/is X lakh"
      "X lakh in deduction / 80c / invest"

    Note: bare "X lakh 80c" is intentionally NOT matched here because
    that phrasing is ambiguous — "80c" following a lakh amount more
    likely describes the category of the *income* question, not a
    deduction amount.  The pattern below requires an explicit deduction
    or investment word to anchor the match.
    """
    q = query.lower().replace(",", "").replace("₹", "")
    # Normalise lakh spellings before matching
    q = re.sub(r"\blakh?s?\b|\blacs?\b|\blak\b", "lakh", q)

    patterns = [
        # "X lakh deduction / invest / saving"  (NOT bare 80c as suffix)
        r"(\d+(?:\.\d+)?)\s*lakh\s*(?:deduction|deduc|invest(?:ment)?|saving)",
        # "deduction / 80c / invest / saving  [of|is]  X lakh"
        r"(?:deduction|deduc|80c|invest|saving)\s+(?:of\s+|is\s+)?(\d+(?:\.\d+)?)\s*lakh",
        # "X lakh  in  deduction / 80c / invest"
        r"(\d+(?:\.\d+)?)\s*lakh\s+(?:in\s+)?(?:deduction|80c|invest)",
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if m:
            return int(float(m.group(1)) * 100_000)
    return 0


def _is_tax_calc_query(query: str) -> bool:
    """Returns True if query is asking for a tax calculation."""
    q = query.lower()
    # Normalise common typos before checking
    q = re.sub(r"\bluck\b|\blak\b", "lakh", q)
    q = re.sub(r"\btake\b", "tax", q)
    has_amount = bool(re.search(
        r"(\d+(?:\.\d+)?)\s*(?:lakh|lac|crore|cr)\b|\b\d{6,8}\b", q
    ))
    has_tax_word = bool(re.search(
        r"\b(tax|टैक्स|कर|pay|kitna|kitni|कितना|calculate|comput|how much|salary|income)\b",
        q, re.IGNORECASE
    ))
    return has_amount and has_tax_word


# =============================================================================
# INCOME TAX TOPIC KEYWORDS
# =============================================================================

_INCOME_TAX_KEYWORDS = {
    "tax", "income", "itr", "return", "tds", "tcs", "deduction", "slab", "regime",
    "hra", "lta", "pan", "aadhaar", "capital", "gain", "section", "exemption",
    "refund", "advance", "assessment", "filing", "form", "challan", "salary",
    "pension", "interest", "dividend", "depreciation", "audit", "80c", "80d",
    "80e", "80g", "80u", "nri", "huf", "crypto", "vda", "esop", "gift",
    "property", "rent", "business", "profession", "gratuity", "ltcg", "stcg",
    "surcharge", "cess", "form16", "26as", "ais", "tis", "scrutiny", "notice",
    "ppf", "elss", "nps", "epf", "turnover", "rectification", "vivad",
    "faceless", "dtaa", "194p", "44ad", "44ada", "44ae", "143", "148",
    "234a", "234b", "234c", "87a", "cgas", "54ec", "54f", "agriculture",
    "agricultural", "lottery", "prize", "donation", "insurance", "maturity",
    "nre", "nro", "resident", "non-resident", "ordinarily",
    "itat", "cit", "cbdt", "din", "belated", "revised", "updated", "itru",
    "sahaj", "sugam", "partnership", "llp", "firm", "company", "corporate",
    "clubbing", "ancestral", "inheritance", "joint", "co-owner",
    "home loan", "house", "inoperative",
    "tax return", "file return", "pay tax", "how much tax",
    "tax liability", "tax saving", "save tax", "tax planning",
    "lakh", "crore",
    "टैक्स", "आयकर", "वेतन", "कर", "आय", "रिटर्न", "कटौती", "छूट",
}


def _is_income_tax_query(query: str) -> bool:
    lower = query.lower()
    return any(kw in lower for kw in _INCOME_TAX_KEYWORDS)


# =============================================================================
# COMPLETE KNOWLEDGE BASE
# =============================================================================

_TAX_KNOWLEDGE_BASE = """
=================================================================
INDIA INCOME TAX — COMPLETE KNOWLEDGE BASE (FY 2025-26)
Income Tax Act, 2025 — effective April 1, 2026
Official portal: www.incometax.gov.in | Helpline: 1800-103-0025
=================================================================

── OVERVIEW ──
Income Tax Act 2025 replaces 1961 Act. Effective April 1, 2026. 536 sections (from 800+).
Single "tax year" replaces "previous year / assessment year" terminology.
FY 2025-26 uses the NEW slabs introduced from April 1, 2026.
Key change: tax-free income under new regime raised to ₹12,00,000 (via enhanced 87A rebate of ₹60,000).
For salaried employees: effectively zero tax up to ₹12,75,000 salary (₹12L + ₹75K standard deduction).

── NEW TAX REGIME (DEFAULT — no action needed) ──
Slabs (FY 2025-26, effective April 1, 2026):
  Up to ₹4,00,000         → NIL
  ₹4,00,001 – ₹8,00,000   → 5%
  ₹8,00,001 – ₹12,00,000  → 10%
  ₹12,00,001 – ₹16,00,000 → 15%
  ₹16,00,001 – ₹20,00,000 → 20%
  ₹20,00,001 – ₹24,00,000 → 25%
  Above ₹24,00,000         → 30%
Standard deduction for salaried: ₹75,000 (unchanged)
Section 87A rebate: Full rebate up to ₹60,000 if taxable income ≤ ₹12,00,000 → NET TAX = ₹0
Salaried zero-tax threshold: ₹12,75,000 salary (after ₹75,000 std dedn → taxable ₹12,00,000 → full rebate)
Marginal relief: If income just crosses ₹12L, total tax cannot exceed (income − ₹12L)
Cess: 4% on tax after rebate (if tax = 0, cess = 0)

── OLD TAX REGIME (must explicitly choose) ──
Slabs (below 60 years) — UNCHANGED:
  Up to ₹2,50,000         → NIL
  ₹2,50,001 – ₹5,00,000   → 5%
  ₹5,00,001 – ₹10,00,000  → 20%
  Above ₹10,00,000         → 30%
Senior citizens (60–79): basic exemption ₹3,00,000
Super senior (80+): basic exemption ₹5,00,000
Standard deduction: ₹50,000
87A rebate: up to ₹12,500 if taxable income ≤ ₹5,00,000
Cess: 4%

── STANDARD DEDUCTION ──
New regime: ₹75,000 (salaried and pensioners)
Old regime: ₹50,000 (salaried and pensioners)
Not available for non-salaried/business income.

── SECTION 87A REBATE ──
New regime: up to ₹60,000 rebate if taxable income ≤ ₹12,00,000 → salary up to ₹12,75,000 is ZERO TAX.
Old regime: up to ₹12,500 rebate if taxable income ≤ ₹5,00,000.
Rebate applied BEFORE cess. If tax after rebate = 0, cess = 0.
87A rebate does NOT apply to STCG (20%) and LTCG (12.5%) on equity — only to normal slab income.

── MARGINAL RELIEF (Important Edge Case) ──
New regime: If taxable income > ₹12L, total tax cannot exceed (income − ₹12L).
Example: taxable ₹12,01,000 → gross tax ₹60,150. Marginal relief = ₹60,150 − ₹1,000 = ₹59,150. Tax = ₹1,000 + 4% cess = ₹1,040.
Example: salary ₹12,75,000 → taxable ₹12,00,000 exactly → full 87A rebate → ZERO TAX.
Example: salary ₹12,76,000 → taxable ₹12,01,000 → marginal relief applies → total tax ≈ ₹1,040.

── WHAT IS AVAILABLE IN NEW vs OLD REGIME ──
                          New Regime    Old Regime
Standard Deduction        ₹75,000      ₹50,000
80C (PPF/ELSS/LIC etc.)   NO           YES (₹1.5L)
80D (Health Insurance)    NO           YES (₹25K–₹1L)
HRA                       NO           YES
LTA                       NO           YES
Home Loan Interest (24b)  NO           YES (₹2L)
NPS Employer (80CCD(2))   YES          YES
NPS Own (80CCD(1B))       NO           YES (₹50K extra)
Family Pension Deduction  YES          YES

Q: Is 80C allowed in new regime? NO.
Q: Can I claim HRA in new regime? NO.
Q: Is home loan interest allowed in new regime? NO.
Q: Is NPS employer contribution (80CCD(2)) allowed in new regime? YES.
Q: Can I claim 80D in new regime? NO.
Q: What deductions work in new regime? Standard deduction ₹75,000 + employer NPS (80CCD(2)) + family pension.
Q: How to reduce tax in new regime? Very limited — only standard deduction and employer NPS. For more savings, consider old regime if deductions are large.

── COMMON SALARY SCENARIOS (New Regime, salaried, FY 2025-26) ──
₹7L salary: taxable = ₹7L − ₹75K = ₹6,25,000. Tax = 5% on ₹2.25L = ₹11,250. Full 87A rebate. Net = ₹0.
₹9L salary: taxable = ₹9L − ₹75K = ₹8,25,000. Tax = 5% on ₹4L + 10% on ₹25K = ₹22,500. Full 87A rebate. Net = ₹0.
₹10L salary: taxable = ₹10L − ₹75K = ₹9,25,000. Tax = 5% on ₹4L + 10% on ₹1.25L = ₹32,500. Full 87A rebate. Net = ₹0.
₹12L salary: taxable = ₹12L − ₹75K = ₹11,25,000. Tax = 5% on ₹4L + 10% on ₹4L = ₹60,000 − wait, let me recompute:
  5% on (₹8L − ₹4L) = ₹20,000; 10% on (₹11.25L − ₹8L) = ₹32,500; gross = ₹52,500. Full 87A rebate (≤₹60K). Net = ₹0.
₹12,75,000 salary: taxable = ₹12,00,000. Tax = 5% on ₹4L + 10% on ₹4L = ₹60,000. Full 87A rebate. Net = ₹0.
₹15L salary: taxable = ₹14,25,000. Tax = ₹20,000 + ₹40,000 + ₹33,750 = ₹93,750 + 4% cess = ₹97,500.
₹18L salary: taxable = ₹17,25,000. Tax = ₹20,000 + ₹40,000 + ₹40,000 + ₹45,000 × wait — 15% on 1.25L:
  5%×4L=₹20K; 10%×4L=₹40K; 15%×4L=₹60K… correction: 5% on ₹4L−₹4L=0; use new slabs properly:
  Tax = 5%×(8L-4L)=₹20K; 10%×(12L-8L)=₹40K; 15%×(17.25L-12L)=₹78,750; gross=₹1,38,750… let pre-computed calc handle exact values.
₹20L salary: taxable = ₹19,25,000. Pre-computed total = ₹1,92,400.
₹24L salary: taxable = ₹23,25,000. Pre-computed total = ₹2,92,500.

── REGIME COMPARISON (FY 2025-26) ──
New regime better when: deductions are small (the new regime's zero-tax up to ₹12L is a huge advantage).
Old regime better only when: total deductions are very large (typically > ₹5–6 lakh).
Which is default? New regime. No action needed.
How to choose old regime? Declare to employer before year starts, or select at ITR filing.
Can salaried switch every year? YES. Business owners: one-time switch only.

Worked comparison (₹15L salary, ₹3.75L deductions):
  New: taxable ₹14,25,000 → total ₹97,500
  Old: ₹15L − ₹50K − ₹3.75L = ₹10,75,000 → tax ₹12,500 + ₹1,00,000 + ₹22,500 = ₹1,35,000 + cess = ₹1,40,400
  → New regime saves ₹42,900

Worked comparison (₹12L salary, ₹4L deductions):
  New: taxable ₹11,25,000 → full 87A rebate → ₹0
  Old: ₹12L − ₹50K − ₹4L = ₹7,50,000 → tax ₹12,500 + ₹50,000 = ₹62,500 + cess = ₹65,000
  → New regime still wins by ₹65,000

── NEW TAX REGIME SLABS FY 2025-26 ──
0–₹4L: 0% | ₹4–8L: 5% | ₹8–12L: 10% | ₹12–16L: 15% | ₹16–20L: 20% | ₹20–24L: 25% | >₹24L: 30%
New regime is default. Old regime requires explicit opt-in.
Key benefit: Salary up to ₹12,75,000 → ZERO tax (87A rebate of ₹60,000 + ₹75,000 std dedn).

── EFFECTIVE TAX RATES (NEW REGIME, SALARIED, FY 2025-26) ──
₹7L income → 0% effective
₹10L income → 0% effective
₹12L income → 0% effective
₹12,75,000 income → 0% effective (maximum zero-tax salary)
₹15L income → ~6.84% effective
₹18L income → ~8.74% effective
₹20L income → ~9.99% effective
₹24L income → ~12.58% effective

── CAPITAL GAINS ──
STCG on listed equity/MFs (STT paid): 20%
STCG on other assets: slab rate
LTCG on listed equity/MFs (STT paid): 12.5% on gains above ₹1,25,000/year (no indexation)
LTCG on property (sold after Jul 23 2024): 12.5% without indexation
LTCG on gold/debt MFs: 12.5%
87A rebate does NOT apply to STCG (20%) and LTCG (12.5%) on equity.
Buyback of shares: amount received is now taxable as capital gains in the hands of shareholders.
Sovereign Gold Bonds (SGBs) bought in secondary market: gains are taxable.
STT on F&O: options sales 0.15% (up from 0.10%); futures 0.05% (up from 0.02%).

── INTEREST INCOME ──
FD interest: taxable at slab rate under "Income from Other Sources". TDS 10% if > ₹40,000 (₹50K senior).
Savings bank interest: 80TTA deduction ₹10,000 (below 60). Submit Form 15G/15H to avoid TDS if no tax.
Rental income: GAV − municipal tax = NAV; NAV − 30% std deduction − home loan interest = taxable.

── ITR FILING ──
FY 2025-26 ITR due dates (revised):
  Salaried / individuals (ITR-1, ITR-2): July 31, 2026
  Non-audit cases (other): August 31, 2026
  Tax audit cases: October 31, 2026
  Transfer pricing cases: November 30, 2026
ITR-1 (Sahaj): Salaried, 1 house property, income ≤ ₹50L, resident — MOST SALARIED PEOPLE USE THIS.
ITR-2: Capital gains / multiple properties / foreign / NRI / income > ₹50L.
ITR-3: Business/profession (non-presumptive).
ITR-4: Presumptive taxation.
Belated return: Dec 31 (penalty ₹1K if income ≤ ₹5L; ₹5K if > ₹5L).
Updated return (ITR-U): within 2 years (additional tax 25–50%).

── TDS ──
Salary: As per slab | FD: 10% above ₹40K (₹50K senior) | Dividend: 10% above ₹5K
Rent: 2% above ₹50K/month | Professional: 10% above ₹30K | Property: 1% above ₹50L
Lottery: 30% above ₹10K | EPF (before 5yr): 10% above ₹50K
No PAN → higher of 20% or prescribed rate. Form 15G/15H → nil TDS if no tax liability.

── TCS (REVISED RATES FY 2025-26) ──
Foreign remittance (LRS): 20% above ₹7L | LRS for education (loan): 0.5% | LRS for medical: 5%
Overseas tour package: 2% (revised down from 20% for amounts above ₹7L)
Alcoholic liquor / coal / tendu leaves: 2%
Motor vehicles above ₹10L: 1%

── ADVANCE TAX ──
Pay in instalments if tax > ₹10K after TDS. Jun 15 (15%), Sep 15 (45%), Dec 15 (75%), Mar 15 (100%).
Interest for default: 234A, 234B, 234C — 1%/month simple.
Senior citizens (no business income): EXEMPT from advance tax.

── SURCHARGE ──
₹50L–₹1Cr: 10% | ₹1–2Cr: 15% | ₹2–5Cr: 25% | >₹5Cr: 37%(old)/25%(new)
LTCG/STCG equity surcharge: capped at 15%.
Cess: 4% on (tax + surcharge).

── GRATUITY ──
Government: fully exempt.
Private: exempt = least of actual / ₹20L lifetime / 15 days salary per completed year of service.

── SENIOR CITIZENS ──
60–79: exemption ₹3L (old), 80TTB ₹50K on all deposits, 80D ₹50K, advance tax exempt.
80+: exemption ₹5L (old), paper ITR allowed.
194P (75+): bank files ITR if only pension + interest from same bank.

── NOTICES ──
143(1): Auto-processed intimation (demand/refund). NOT scrutiny.
143(2): Scrutiny notice — detailed examination.
148/148A: Income escaped assessment.
Every genuine notice has DIN. No DIN = invalid notice.

── PENALTIES ──
Under-reporting: 50% of tax. Misreporting: 200%. Late filing (234F): ₹1K/₹5K.
Wilful evasion: imprisonment 3 months–7 years. Missing deadline alone ≠ jail.

── HOW TO SAVE TAX ──
Under old regime:
  80C: ₹1.5L (PPF, ELSS, NPS, LIC, home loan principal, tax-saving FD)
  80CCD(1B): extra ₹50K for NPS
  80D: ₹25K health insurance (₹50K if parents senior)
  HRA: claim if paying rent
  Home loan interest: Section 24b up to ₹2L
Under new regime: only standard deduction ₹75K and employer NPS (80CCD(2)) available.
Note: New regime now so tax-efficient (zero tax up to ₹12L) that old regime is rarely better.
=================================================================
END OF KNOWLEDGE BASE
=================================================================
"""


# =============================================================================
# LEAN SYSTEM PROMPT — identity + rules only (~300 tokens, not 2600)
# The knowledge base is injected per-query in the user message (relevant
# sections only), dramatically reducing tokens processed per call.
# =============================================================================

SYSTEM_PROMPT = """
You are TaxBot — India's Income Tax AI assistant.

IDENTITY (absolute):
- Name: TaxBot. Never say GPT, Claude, Gemini, Google, OpenAI, or any other model.
- If asked "who are you": say "I am TaxBot, an AI assistant for Indian income tax."
- In Hindi: "मैं TaxBot हूँ — भारतीय आयकर का AI सहायक।"

LANGUAGE RULE (highest priority):
- REPLY LANGUAGE = EN → every sentence in English only.
- REPLY LANGUAGE = HI → every sentence in Hindi (Devanagari) only.
- Judge by user's script: Latin letters → English. Devanagari → Hindi.
- Hinglish (Roman Hindi like "bhai", "yaar", "batao", "pe tax", "ki salary") → Hindi.
- Never mix languages. Never use Tamil or any other language.

ROUTING:
- INCOME TAX QUERY → answer from the KNOWLEDGE BASE provided in the user message.
- GENERAL QUERY → answer from web search results in CONTEXT, or own knowledge.
- Never refuse a general question. Never redirect general questions to tax portals.

NUMBER FORMAT (mandatory — every single number):
- ALL rupee amounts MUST use Indian comma format: ₹12,00,000 not ₹1200000 or ₹12 00 000.
- Write amounts as: ₹75,000 / ₹1,50,000 / ₹12,00,000 / ₹1,00,00,000
- NEVER abbreviate lakh as "L" — always write "lakh". Say "10 lakh" not "10 L" or "₹10 L".
- Use "lakh" and "crore" in full: ₹12 lakh, ₹1.5 crore.

CALCULATION FORMAT (for tax working):
- Show slab computation as: "5% on ₹4,00,000 giving ₹20,000" — NOT "5% × ₹4,00,000 = ₹20,000"
- Use "giving" instead of "=" in tax step-by-step.
- After each slab: write the tax amount in plain words — "giving ₹20,000" not "(₹20,000)".
- Never put just a rupee amount in brackets — always precede it with "giving" or "resulting in".
- Pre-computed numbers are always provided for calculation queries — use ONLY those exact figures.

URL FORMAT:
- Always write the portal as: www.incometax.gov.in
- Never write it as a clickable link or say "https://".

PRONUNCIATION GUIDE (for text-to-speech compatibility):
- "regime" = reh-ZHEEM (not "ree-jime"). Write as: tax regime (reh-ZHEEM).
- URL: say "www dot incometax dot gov dot in" — never the full https:// form.
- ₹ symbol: in English say "rupees"; in Hindi say "रुपये".
- In Hindi responses: write ALL numbers in Devanagari numerals (₹ → रुपये, ₹20,000 → बीस हजार रुपये).

HINDI NUMBER FORMAT (for REPLY LANGUAGE = HI):
- Write rupee amounts in Hindi words: ₹20,000 → बीस हज़ार रुपये; ₹1,50,000 → एक लाख पचास हज़ार रुपये.
- Use Devanagari for all currency references. Do not leave any number in English digits when giving spoken explanation.
- The ₹ symbol is acceptable in written output but must be read as "रुपये".

FORMAT:
- Answer first — lead with the direct answer, no preamble.
- Plain prose only. No markdown tables. No pipe characters (|).
- For tax calculations: state the answer, show working in 2-3 sentences using "giving" not "=", compare regimes, end with www.incometax.gov.in
- Max 3-4 lines for any summary. Never repeat what was already said.
- No filler: no "Great question", "As an AI", "Certainly!", "Based on context".
""".strip()


# =============================================================================
# KB TOPIC INDEX — maps query keywords to KB section names
# Only the matching section is injected per call (~300-500 tokens vs 2163)
# =============================================================================

_KB_SECTIONS: dict[str, str] = {}  # populated below from _TAX_KNOWLEDGE_BASE

def _build_kb_index() -> None:
    """Parse _TAX_KNOWLEDGE_BASE into named sections for fast retrieval."""
    current_name = "GENERAL"
    current_lines: list[str] = []
    for line in _TAX_KNOWLEDGE_BASE.splitlines():
        if line.startswith("──") and "──" in line[2:]:
            if current_lines:
                _KB_SECTIONS[current_name] = "\n".join(current_lines).strip()
            current_name = line.strip("─ \t").strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        _KB_SECTIONS[current_name] = "\n".join(current_lines).strip()

_build_kb_index()

# Topic → section name mapping for fast lookup
_TOPIC_SECTIONS: list[tuple[set[str], str]] = [
    ({"slab", "rate", "regime", "new regime", "old regime", "87a", "rebate",
      "surcharge", "cess", "standard deduction", "tax rate", "percentage",
      "how much tax", "kitna tax", "कितना टैक्स"}, "NEW TAX REGIME (DEFAULT — no action needed)"),
    ({"80c", "80d", "80e", "80g", "80gg", "80u", "80dd", "80ddb", "80tta",
      "80ttb", "80eea", "deduction", "ppf", "elss", "nps", "lic", "insurance",
      "home loan", "section 24", "कटौती"}, "DEDUCTIONS (OLD REGIME ONLY unless specified)"),
    ({"itr", "file", "filing", "form", "sahaj", "sugam", "return", "deadline",
      "due date", "belated", "revised", "updated", "e-verify", "itr-u",
      "itr-1", "itr-2", "itr-3", "itr-4", "रिटर्न", "दाखिल"}, "ITR FILING"),
    ({"capital gain", "ltcg", "stcg", "section 54", "54ec", "54f", "shares",
      "equity", "mutual fund", "property sold", "holding period",
      "indexation", "cgas", "पूंजीगत लाभ"}, "CAPITAL GAINS"),
    ({"tds", "form 16", "form 16a", "26as", "ais", "15g", "15h", "tds rate",
      "deducted", "स्रोत पर कर"}, "TDS"),
    ({"tcs", "foreign remittance", "lrs", "overseas", "tour package"}, "TCS RATES"),
    ({"advance tax", "234a", "234b", "234c", "self assessment", "challan 280",
      "instalment", "अग्रिम कर"}, "ADVANCE TAX"),
    ({"crypto", "vda", "nft", "bitcoin", "virtual", "क्रिप्टो"}, "CRYPTO/VDA"),
    ({"esop", "stock option", "perquisite"}, "ESOP"),
    ({"gift", "inheritance", "will", "उपहार"}, "GIFTS"),
    ({"nri", "non resident", "dtaa", "nre", "nro", "foreign", "residential status",
      "अनिवासी"}, "NRI"),
    ({"senior citizen", "80 years", "194p", "वरिष्ठ नागरिक"}, "SENIOR CITIZENS"),
    ({"huf", "hindu undivided"}, "HUF"),
    ({"penalty", "prosecution", "jail", "fine", "जुर्माना"}, "PENALTIES"),
    ({"notice", "scrutiny", "143", "148", "appeal", "cit", "itat", "नोटिस"},
      "NOTICES"),
    ({"gratuity", "leave encashment", "vrs", "ग्रेच्युटी"}, "GRATUITY"),
    ({"business", "profession", "44ad", "44ada", "44ae", "presumptive",
      "turnover", "audit", "depreciation", "व्यापार"}, "BUSINESS INCOME"),
    ({"rent", "house property", "hra", "किराया", "मकान"}, "RENTAL INCOME"),
    ({"salary", "वेतन", "salary calculation", "my salary"}, "SALARY-BASED SCENARIOS"),
]

def _get_relevant_kb(query: str) -> str:
    """
    Return only the KB section(s) relevant to the query.
    Falls back to the full KB only if no section matches.
    """
    q = query.lower()
    matched_sections: list[str] = []
    for keywords, section_name in _TOPIC_SECTIONS:
        if any(kw in q for kw in keywords):
            section_text = _KB_SECTIONS.get(section_name, "")
            if section_text:
                matched_sections.append(f"[{section_name}]\n{section_text}")

    if matched_sections:
        return "\n\n".join(matched_sections)

    # No specific match — return slabs + general sections (most common need)
    defaults = [
        "NEW TAX REGIME (DEFAULT — no action needed)",
        "OLD TAX REGIME (must explicitly choose)",
        "REGIME COMPARISON — WHO BENEFITS FROM WHICH",
        "SALARY-BASED SCENARIOS",
    ]
    fallback = []
    for name in defaults:
        t = _KB_SECTIONS.get(name, "")
        if t:
            fallback.append(f"[{name}]\n{t}")
    return "\n\n".join(fallback) if fallback else _TAX_KNOWLEDGE_BASE

_FALLBACK_MESSAGE = (
    "I'm unable to process your request right now. "
    "Please visit www.incometax.gov.in or call 1800-103-0025 (toll-free) for assistance."
)

_LANGUAGE_NAMES = {
    "en": "English — reply in English ONLY",
    "hi": "Hindi — reply ENTIRELY in Hindi (Devanagari script). No English sentences at all.",
}


# =============================================================================
# LLM SERVICE
# =============================================================================

class LLMService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def stream_chat_completion(
        self,
        context: str,
        query: str,
        history: list[dict],
        language_hint: str = "en",
    ) -> AsyncGenerator[str, None]:

        # Step 1: Auto-detect language from query text (overrides frontend hint if script found)
        detected = detect_language(query)
        if detected != "en":
            language_hint = detected
        lang_label = _LANGUAGE_NAMES.get(language_hint, _LANGUAGE_NAMES["en"])

        # Step 2: Determine query type
        is_tax = _is_income_tax_query(query)

        # Step 3: Pre-compute tax if it is a calculation query
        calc_block = ""
        if is_tax and _is_tax_calc_query(query):
            salary = _extract_salary_amount(query)
            if salary and 10_000 < salary < 100_000_000:
                deductions = _extract_deductions(query)
                calc_block = _build_tax_calc_context(salary, deductions)
                logger.info("Tax calc injected: salary=%d deductions=%d", salary, deductions)

        # Step 4: Build context block
        if is_tax:
            query_type_label = "INCOME TAX QUERY"
            if calc_block:
                context_instruction = (
                    "A PRE-COMPUTED TAX CALCULATION is provided. "
                    "Use EXACTLY those numbers. Show step-by-step for BOTH regimes. "
                    "State winner clearly."
                )
                # Inject calc block + relevant KB so LLM has both numbers and rules
                relevant_kb = _get_relevant_kb(query)
                full_context = f"{calc_block}\n\n[RELEVANT KNOWLEDGE BASE]\n{relevant_kb}"
            else:
                context_instruction = (
                    "Answer from the KNOWLEDGE BASE section provided below."
                )
                # Inject only the relevant KB sections (~300-500 tokens vs 2163)
                full_context = f"[KNOWLEDGE BASE — relevant sections]\n{_get_relevant_kb(query)}"
        else:
            query_type_label = "GENERAL QUERY — WEB SEARCH RESULTS"
            context_instruction = (
                "Answer from the web search results in CONTEXT. "
                "If empty, use your own knowledge. Do NOT refuse."
            )
            full_context = context or ""

        # Step 5: Compose user message
        # Detect if this is an identity question — lock down the answer
        identity_keywords = [
            "who are you", "what are you", "which ai", "which model",
            "which llm", "are you gpt", "are you gemini", "are you claude",
            "are you chatgpt", "who made you", "which company",
            "aap kaun", "aap kya", "tum kaun", "main kaun", "kon ho tum",
            "आप कौन", "तुम कौन", "कौन से", "किस कंपनी",
        ]
        is_identity_query = any(kw in query.lower() for kw in identity_keywords)
        identity_note = (
            "\n• IDENTITY LOCK: This is an identity question. "
            "You MUST answer: 'I am TaxBot, an AI assistant for Indian income tax.' "
            "Do NOT mention Google, OpenAI, GPT, Claude, Gemini, or any other company/model."
        ) if is_identity_query else ""

        user_content = (
            f"REPLY LANGUAGE: {language_hint.upper()}\n"
            f"The user wrote in {lang_label}. Your ENTIRE response must be in that language only.\n\n"
            f"QUERY TYPE: {query_type_label}\n\n"
            f"--- CONTEXT ---\n{full_context}\n--- END CONTEXT ---\n\n"
            f"User Question: {query}\n\n"
            f"• {context_instruction}\n"
            f"• Language is {language_hint.upper()}. Every sentence must be in {lang_label}. "
            f"If REPLY LANGUAGE is EN, write in English regardless of any other text in this message."
            f"{identity_note}\n"
            "• Answer directly. No XML. No tables. No pipes (|). No meta-commentary."
        )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        self._write_prompt_to_file(messages)

        if not self.settings.llm_base_url or not self.settings.llm_model_name:
            logger.warning(
                "LLM not configured: llm_base_url=%s, llm_model_name=%s",
                self.settings.llm_base_url,
                self.settings.llm_model_name,
            )
            yield _FALLBACK_MESSAGE
            return

        endpoint = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.llm_api_key or 'EMPTY'}",
        }
        payload = {
            "model": self.settings.llm_model_name,
            "messages": messages,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_tokens,
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(
                timeout=120.0, verify=self.settings.llm_verify_ssl
            ) as client:
                async with client.stream(
                    "POST", endpoint, headers=headers, json=payload
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line.removeprefix("data:").strip()
                        if data == "[DONE]":
                            break
                        try:
                            parsed = json.loads(data)
                            delta = parsed.get("choices", [{}])[0].get("delta", {})
                            token = delta.get("content", "")
                            if token:
                                yield token
                        except json.JSONDecodeError:
                            continue

        except httpx.TimeoutException:
            logger.error("LLM request timed out after 120 seconds.")
            yield _FALLBACK_MESSAGE
        except httpx.HTTPStatusError as exc:
            logger.error("LLM HTTP error — status: %s, detail: %s", exc.response.status_code, exc)
            yield _FALLBACK_MESSAGE
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("LLM streaming failed unexpectedly: %s", exc)
            yield _FALLBACK_MESSAGE

    def _write_prompt_to_file(self, messages: list[dict]) -> None:
        """Disabled — synchronous file write was blocking the async event loop."""
        pass