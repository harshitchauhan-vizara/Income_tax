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
    Returns 'hi' or 'en' only.
    """
    if re.search(r"[\u0900-\u097F]", text):
        return "hi"
    if re.search(r"[\u0600-\u06FF]", text):
        return "hi"
    hindi_only = (
        r"\b(kya|kitna|kitni|kaisa|kaise|kyun|kyunki|lagega|batao|samjhao|"
        r"mujhe|mujhko|rupaye|paisa|wala|wali|"
        r"yeh\s+salary|mera\s+salary|meri\s+income|kitna\s+tax|"
        r"kaise\s+bhare|bharna\s+hai|"
        r"bhai|yaar|"
        r"mere\s+liye|konsa|konsi|kaunsa|kaunsi|"
        r"karo|lagta\s+hai|lagti\s+hai|hoga|hogi|"
        r"pe\s+(?:tax|old|new|kitna)|ki\s+salary|ka\s+tax|"
        r"naya\s+regime|purana\s+regime|nai\s+regime|"
        r"tax\s+kitna|tax\s+batao|salary\s+pe|income\s+pe)\b"
    )
    if re.search(hindi_only, text, re.IGNORECASE):
        return "hi"
    return "en"


# =============================================================================
# HARDCODED INCOME TAX CALCULATOR
# FY 2026-27 | AY 2027-28 | Section 115BAC (New Tax Regime)
# Finance Act, 2026 — slabs UNCHANGED from prior year
# =============================================================================

def _compute_tax_new_regime(taxable_income: int) -> dict:
    """
    New Tax Regime — Section 115BAC (FY 2026-27 / AY 2027-28):
    Finance Act 2026 confirms slabs UNCHANGED:
      Up to Rs.4,00,000      -> 0%
      Rs.4,00,001-Rs.8,00,000  -> 5%
      Rs.8,00,001-Rs.12,00,000 -> 10%
      Rs.12,00,001-Rs.16,00,000-> 15%
      Rs.16,00,001-Rs.20,00,000-> 20%
      Rs.20,00,001-Rs.24,00,000-> 25%
      Above Rs.24,00,000      -> 30%
    87A rebate: up to Rs.60,000 if taxable income <= Rs.12,00,000
    Marginal relief if just over Rs.12L
    Cess: 4%
    """
    slabs = [
        (400_000,      0.00),
        (800_000,      0.05),
        (1_200_000,    0.10),
        (1_600_000,    0.15),
        (2_000_000,    0.20),
        (2_400_000,    0.25),
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
                "from": prev,
                "to": int(min(taxable_income, limit)),
                "rate": rate,
                "tax": slab_tax,
            })
        tax += slab_tax
        prev = int(min(limit, float("inf"))) if limit != float("inf") else prev + chunk

    rebate = 0
    marginal_relief = 0

    if taxable_income <= 1_200_000:
        rebate = min(tax, 60_000)
        tax_after = tax - rebate
    else:
        tax_after = tax
        excess = taxable_income - 1_200_000
        if tax_after > excess:
            marginal_relief = tax_after - excess
            tax_after = excess

    cess = int(tax_after * 0.04)
    total = tax_after + cess
    eff = round(total / taxable_income * 100, 2) if taxable_income > 0 else 0.0
    return {
        "gross_tax": tax,
        "rebate_87a": rebate,
        "marginal_relief": marginal_relief,
        "tax_after_rebate": tax_after,
        "cess": cess,
        "total_tax": total,
        "effective_rate": eff,
        "slab_breakdown": breakdown,
    }


def _compute_tax_old_regime(taxable_income: int, age: int = 30) -> dict:
    """
    Old Tax Regime slabs (FY 2026-27 / AY 2027-28) — UNCHANGED:
      Below 60: 0-Rs.2.5L:0%, Rs.2.5-5L:5%, Rs.5-10L:20%, >Rs.10L:30%
      60-79:    0-Rs.3L:0%,   Rs.3-5L:5%,   Rs.5-10L:20%, >Rs.10L:30%
      80+:      0-Rs.5L:0%,   Rs.5-10L:20%, >Rs.10L:30%
    87A rebate: up to Rs.12,500 if taxable income <= Rs.5,00,000
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
                "from": prev,
                "to": int(min(taxable_income, limit)),
                "rate": rate,
                "tax": slab_tax,
            })
        tax += slab_tax
        prev = int(min(limit, float("inf"))) if limit != float("inf") else prev + chunk

    rebate = min(tax, 12_500) if taxable_income <= 500_000 else 0
    tax_after = tax - rebate
    cess = int(tax_after * 0.04)
    total = tax_after + cess
    eff = round(total / taxable_income * 100, 2) if taxable_income > 0 else 0.0
    return {
        "gross_tax": tax,
        "rebate_87a": rebate,
        "tax_after_rebate": tax_after,
        "cess": cess,
        "total_tax": total,
        "effective_rate": eff,
        "slab_breakdown": breakdown,
    }


def _fmt(n: int) -> str:
    """Format as Indian currency: 1500000 -> Rs.15,00,000"""
    if n == 0:
        return "Rs.0"
    s = str(abs(n))
    if len(s) <= 3:
        return f"Rs.{s}"
    last3 = s[-3:]
    rest = s[:-3]
    groups = []
    while len(rest) > 2:
        groups.append(rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.append(rest)
    groups.reverse()
    return f"Rs.{','.join(groups)},{last3}"


def _build_tax_calc_context(gross_salary: int, deductions: int = 0) -> str:
    """
    Pre-computed tax calculation block.
    FY 2026-27 | AY 2027-28 | Finance Act, 2026
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
        "Financial Year two thousand twenty six to twenty seven | Assessment Year two thousand twenty seven to twenty eight | Finance Act, two thousand twenty six | Section 115BAC",
        f"Input: Gross Salary = {_fmt(gross_salary)}"
        + (f"  |  Extra Deductions = {_fmt(deductions)}" if deductions else ""),
        "",
        "── NEW TAX REGIME — Section 115BAC (Default) ──",
    ]
    out += regime_lines("new", gross_salary, new_std, 0, new_taxable, nr)
    out += ["", "── OLD TAX REGIME (Optional, must be explicitly chosen) ──"]
    out += regime_lines("old", gross_salary, old_std, deductions, old_taxable, or_)
    out += [
        "",
        f"── VERDICT: {winner} saves {_fmt(saving)} more tax ──",
        "=== END OF PRE-COMPUTED CALCULATION ===",
    ]
    return "\n".join(out)


def _extract_salary_amount(query: str) -> int | None:
    q = query.lower().replace(",", "").replace("Rs.", "")
    q = re.sub(r"\bluck\b|\blak\b|\blakh?s?\b|\blacs?\b", "lakh", q)
    q = re.sub(r"\btake\b", "tax", q)

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

    dedn_kw = r"\b(deduction|deductions|invest(?:ment)?|saving|savings|deduc)\b"
    dedn_positions = [m.start() for m in re.finditer(dedn_kw, q, re.IGNORECASE)]

    if dedn_positions:
        working_set = [
            (v, pos) for v, pos in candidates
            if min(abs(pos - dp) for dp in dedn_positions) > 15
        ]
    else:
        working_set = candidates

    if not working_set:
        working_set = candidates
    if len(working_set) == 1:
        return working_set[0][0]

    salary_kw = r"\b(salary|income|earn|earns|earning|ctc|package|pay|paid|\u092e\u0947\u0930\u093e|\u0935\u0947\u0924\u0928)\b"
    salary_positions = [m.start() for m in re.finditer(salary_kw, q, re.IGNORECASE)]

    if salary_positions:
        def _closest_salary_dist(pos: int) -> int:
            return min(abs(pos - sp) for sp in salary_positions)
        working_set.sort(key=lambda x: _closest_salary_dist(x[1]))
        return working_set[0][0]

    return max(v for v, _ in working_set)


def _extract_deductions(query: str) -> int:
    q = query.lower().replace(",", "").replace("Rs.", "")
    q = re.sub(r"\blakh?s?\b|\blacs?\b|\blak\b", "lakh", q)
    patterns = [
        r"(\d+(?:\.\d+)?)\s*lakh\s*(?:deduction|deduc|invest(?:ment)?|saving)",
        r"(?:deduction|deduc|80c|invest|saving)\s+(?:of\s+|is\s+)?(\d+(?:\.\d+)?)\s*lakh",
        r"(\d+(?:\.\d+)?)\s*lakh\s+(?:in\s+)?(?:deduction|80c|invest)",
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if m:
            return int(float(m.group(1)) * 100_000)
    return 0


def _is_tax_calc_query(query: str) -> bool:
    q = query.lower()
    q = re.sub(r"\bluck\b|\blak\b", "lakh", q)
    q = re.sub(r"\btake\b", "tax", q)
    has_amount = bool(re.search(
        r"(\d+(?:\.\d+)?)\s*(?:lakh|lac|crore|cr)\b|\b\d{6,8}\b", q
    ))
    has_tax_word = bool(re.search(
        r"\b(tax|\u091f\u0948\u0915\u094d\u0938|\u0915\u0930|pay|kitna|kitni|\u0915\u093f\u0924\u0928\u093e|calculate|comput|how much|salary|income)\b",
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
    "lakh", "crore", "buyback", "buy back", "prosecution", "decriminal",
    "\u091f\u0948\u0915\u094d\u0938", "\u0906\u092f\u0915\u0930", "\u0935\u0947\u0924\u0928",
    "\u0915\u0930", "\u0906\u092f", "\u0930\u093f\u091f\u0930\u094d\u0928",
    "\u0915\u091f\u094c\u0924\u0940", "\u091b\u0942\u091f",
}


def _is_income_tax_query(query: str) -> bool:
    lower = query.lower()
    return any(kw in lower for kw in _INCOME_TAX_KEYWORDS)


# =============================================================================
# COMPLETE KNOWLEDGE BASE — FY 2026-27 | AY 2027-28
# Incorporating Finance Act, 2026 (Finance Bill 2026) amendments
# =============================================================================

_TAX_KNOWLEDGE_BASE = """
=================================================================
INDIA INCOME TAX — COMPLETE KNOWLEDGE BASE (FY 2026-27 | AY 2027-28)
Income Tax Act, 2025 — effective April 1, 2026
Finance Act, 2026 (Finance Bill 2026, introduced Feb 1, 2026)
Official portal: www.incometax.gov.in | Helpline: 1800-103-0025
=================================================================

── OVERVIEW ──
Income Tax Act 2025 replaces 1961 Act. Effective April 1, 2026. 536 sections.
FY 2026-27 (AY 2027-28) is the FIRST financial year under the Income Tax Act, 2025.
Finance Act, 2026 was introduced on Feb 1, 2026 and governs tax rates for FY 2026-27.
Key benefit: tax-free income under new regime up to Rs.12,00,000 (via 87A rebate of Rs.60,000).
For salaried: zero tax up to Rs.12,75,000 salary (Rs.12,00,000 taxable after Rs.75,000 std dedn).

── NEW TAX REGIME — Section 115BAC (DEFAULT, no action needed) ──
Slabs for FY 2026-27 / AY 2027-28 (Finance Act 2026 — UNCHANGED from prior year):
  Up to Rs.4,00,000          → NIL
  Rs.4,00,001 – Rs.8,00,000    → 5%
  Rs.8,00,001 – Rs.12,00,000   → 10%
  Rs.12,00,001 – Rs.16,00,000  → 15%
  Rs.16,00,001 – Rs.20,00,000  → 20%
  Rs.20,00,001 – Rs.24,00,000  → 25%
  Above Rs.24,00,000          → 30%
Standard deduction for salaried / pensioners: Rs.75,000
Section 87A rebate: Full rebate up to Rs.60,000 if taxable income <= Rs.12,00,000 → NET TAX = Rs.0
Zero-tax salary threshold: Rs.12,75,000 (after Rs.75,000 std dedn → Rs.12,00,000 taxable → full 87A rebate)
Marginal relief: If taxable income > Rs.12L, total tax cannot exceed (taxable income - Rs.12L)
Cess: 4% on tax after rebate/relief (if tax = 0, cess = 0)

── OLD TAX REGIME (must explicitly choose) ──
Slabs for FY 2026-27 / AY 2027-28 (UNCHANGED):
  Below 60 years:
    Up to Rs.2,50,000          → NIL
    Rs.2,50,001 – Rs.5,00,000    → 5%
    Rs.5,00,001 – Rs.10,00,000   → 20%
    Above Rs.10,00,000          → 30%
  Senior citizens (60-79): basic exemption Rs.3,00,000
  Super senior (80+): basic exemption Rs.5,00,000
Standard deduction: Rs.50,000
87A rebate: up to Rs.12,500 if taxable income <= Rs.5,00,000
Cess: 4%

── STANDARD DEDUCTION ──
New regime (Section 115BAC): Rs.75,000 (salaried and pensioners)
Old regime: Rs.50,000 (salaried and pensioners)
Not available for non-salaried / business income.

── SECTION 87A REBATE (FY 2026-27) ──
New regime: up to Rs.60,000 rebate if taxable income <= Rs.12,00,000 → salary up to Rs.12,75,000 is ZERO TAX.
Old regime: up to Rs.12,500 rebate if taxable income <= Rs.5,00,000.
87A rebate does NOT apply to STCG (20%) and LTCG (12.5%) on equity.

── MARGINAL RELIEF ──
New regime: If taxable income > Rs.12L, total tax cannot exceed (taxable income - Rs.12L).
Example: taxable Rs.12,01,000 → gross tax Rs.60,150. Marginal relief = Rs.59,150. Tax = Rs.1,000 + 4% cess = Rs.1,040.

── WHAT IS AVAILABLE IN NEW vs OLD REGIME ──
                          New Regime    Old Regime
Standard Deduction        Rs.75,000      Rs.50,000
80C (PPF/ELSS/LIC etc.)   NO           YES (Rs.1.5L)
80D (Health Insurance)    NO           YES (Rs.25K-Rs.1L)
HRA                       NO           YES
LTA                       NO           YES
Home Loan Interest (24b)  NO           YES (Rs.2L)
NPS Employer (80CCD(2))   YES          YES
NPS Own (80CCD(1B))       NO           YES (Rs.50K extra)
Family Pension Deduction  YES          YES

── COMMON SALARY SCENARIOS (New Regime / Section 115BAC, salaried, FY 2026-27) ──
Rs.7L salary:    taxable=Rs.6,25,000. 5%x(Rs.6.25L-Rs.4L)=Rs.11,250. Full 87A rebate. Net=Rs.0.
Rs.9L salary:    taxable=Rs.8,25,000. 5%xRs.4L=Rs.20,000 + 10%xRs.25K=Rs.2,500. Full 87A rebate. Net=Rs.0.
Rs.10L salary:   taxable=Rs.9,25,000. 5%xRs.4L=Rs.20,000 + 10%xRs.1.25L=Rs.12,500. Full 87A rebate. Net=Rs.0.
Rs.12,75,000:    taxable=Rs.12,00,000. 5%xRs.4L=Rs.20,000 + 10%xRs.4L=Rs.40,000. Gross=Rs.60,000. Full 87A rebate. Net=Rs.0.
Rs.15L salary:   taxable=Rs.14,25,000. 5%xRs.4L+10%xRs.4L+15%xRs.2.25L=Rs.93,750+cess=Rs.97,500.
Rs.18L salary:   taxable=Rs.17,25,000. 5%xRs.4L+10%xRs.4L+15%xRs.4L+20%xRs.1.25L=Rs.1,45,000+cess=Rs.1,50,800.
Rs.20L salary:   taxable=Rs.19,25,000. Pre-computed total approx Rs.1,72,300.
Rs.24L salary:   taxable=Rs.23,25,000. Pre-computed total approx Rs.2,57,400.

── REGIME COMPARISON (FY 2026-27 / AY 2027-28) ──
New regime better when: deductions are small (zero-tax up to Rs.12,75,000 salary is a huge advantage).
Old regime better only when: total deductions are very large (typically > Rs.5-6 lakh combined).
Default: New regime (Section 115BAC). Old regime requires explicit opt-in.
How to choose old regime? Declare to employer before year starts, or select at ITR filing for AY 2027-28.
Salaried: can switch every year. Business owners: one-time switch only.

── CAPITAL GAINS (FY 2026-27) ──
STCG on listed equity/MFs (STT paid): 20%
STCG on other assets: slab rate
LTCG on listed equity/MFs (STT paid): 12.5% on gains above Rs.1,25,000/year (no indexation)
LTCG on property (sold after Jul 23, 2024): 12.5% without indexation
LTCG on gold/debt MFs: 12.5%
87A rebate does NOT apply to STCG (20%) and LTCG (12.5%) on equity.
BUYBACK OF SHARES (FY 2026-27 — Finance Act 2026 change): Under Income Tax Act 2025,
buyback consideration is EXCLUDED from the definition of "dividend" (sub-clause (f) of
Section 2(40) omitted by Finance Act 2026). Buyback proceeds are taxed as CAPITAL GAINS,
not as dividend income, in the hands of shareholders for FY 2026-27.
STT on F&O: options sales 0.15%; futures 0.05%.

── INTEREST INCOME ──
FD interest: taxable at slab rate. TDS 10% if > Rs.40,000 (Rs.50K senior).
Savings bank interest: 80TTA deduction Rs.10,000 (below 60). Form 15G/15H to avoid TDS.
Rental income: GAV - municipal tax = NAV; NAV - 30% std deduction - home loan interest = taxable.

── ITR FILING (FY 2026-27 / AY 2027-28) — Finance Act 2026 ──
Due dates (Section 263, Income Tax Act 2025):
  Salaried / individuals (ITR-1, ITR-2):             July 31, 2027
  Business/profession income (non-audit, non-TP):    August 31, 2027   <- NEW CATEGORY
  Tax audit cases:                                    October 31, 2027
  Transfer pricing cases:                             November 30, 2027
  Belated return:                                     December 31, 2027

REVISED RETURN (Finance Act 2026 — extended):
  Time limit: 12 months from end of relevant tax year (extended from 9 months)
  i.e., revised return for FY 2026-27 can be filed up to March 31, 2028
  Fee if revised return filed AFTER 9 months (Section 428(b)):
    Rs.1,000 if total income <= Rs.5 lakh; Rs.5,000 otherwise

UPDATED RETURN (ITR-U) — Finance Act 2026:
  Window: 48 months (4 years) from end of the financial year succeeding the relevant tax year
  For FY 2026-27 (AY 2027-28): ITR-U can be filed up to March 31, 2032
  Additional tax: 25%-70% of incremental tax depending on year of filing
  If filed in pursuance of notice under Section 148: within period specified in that notice
  Cannot reduce tax liability or claim refund

ITR-1 (Sahaj): Salaried, 1 house property, income <= Rs.50L, resident.
ITR-2: Capital gains / multiple properties / foreign / NRI / income > Rs.50L.
ITR-3: Business/profession (non-presumptive).
ITR-4 (Sugam): Presumptive taxation.
Late filing fee: Rs.1K if income <= Rs.5L; Rs.5K if > Rs.5L.

── TDS ──
Salary: As per slab | FD: 10% above Rs.40K (Rs.50K senior) | Dividend: 10% above Rs.5K
Rent: 2% above Rs.50K/month | Professional: 10% above Rs.30K | Property: 1% above Rs.50L
Lottery: 30% above Rs.10K | EPF (before 5yr): 10% above Rs.50K
No PAN → higher of 20% or prescribed rate. Form 15G/15H → nil TDS if no tax liability.

── TCS (FY 2026-27 RATES) ──
Foreign remittance (LRS): 20% above Rs.7L | LRS for education (loan): 0.5% | LRS for medical: 5%
Overseas tour package: 20% (no threshold)
Motor vehicles above Rs.10L: 1%

── ADVANCE TAX (FY 2026-27) ──
Pay in instalments if tax > Rs.10K after TDS.
June 15, 2026 (15%) | September 15, 2026 (45%) | December 15, 2026 (75%) | March 15, 2027 (100%).
Interest for default: 234A, 234B, 234C — 1%/month simple.
Senior citizens (no business income): EXEMPT from advance tax.

── SURCHARGE (FY 2026-27 — UNCHANGED) ──
Rs.50L-Rs.1Cr: 10% | Rs.1-2Cr: 15% | Rs.2-5Cr: 25% | >Rs.5Cr: 37% (old) / 25% (new, capped)
LTCG/STCG equity surcharge: capped at 15%.
Cess: 4% on (tax + surcharge).

── GRATUITY ──
Government: fully exempt.
Private: exempt = least of actual / Rs.20L lifetime / 15 days salary per completed year of service.

── SENIOR CITIZENS (FY 2026-27) ──
60-79: exemption Rs.3L (old regime), 80TTB Rs.50K on all deposits, 80D Rs.50K, advance tax exempt.
80+: exemption Rs.5L (old regime), paper ITR allowed.
194P (75+): bank files ITR if only pension + interest from same bank.

── NOTICES ──
143(1): Auto-processed intimation (demand/refund). NOT scrutiny.
143(2): Scrutiny notice — detailed examination.
148/148A: Income escaped assessment.
Every genuine notice has DIN. No DIN = invalid notice.
Finance Act 2026: clarifies DIN requirement — assessment order referenced by DIN is valid
even if there is a minor error in quoting the DIN.

── PENALTIES (FY 2026-27) ──
Under-reporting: 50% of tax. Misreporting: 200%. Late filing fee: Rs.1K/Rs.5K.
Finance Act 2026: Penalty for under-reporting of income (Section 270A / Section 439) is now
imposed IN the assessment order itself (for assessment orders made on or after April 1, 2027).
Immunity from penalty available for misreporting cases if 100% additional tax paid.

── PROSECUTION — DECRIMINALIZED (Finance Act 2026) ──
Finance Act 2026 substantially decriminalises many tax offences. Updated punishments:
Wilful tax evasion / failure to file (Section 276C, 276CC, 276B etc.):
  Evasion/TDS default > Rs.50 lakh: Simple imprisonment up to 2 years, OR fine, OR both
  Evasion/TDS default Rs.10-50 lakh: Simple imprisonment up to 6 months, OR fine, OR both
  Evasion/TDS default < Rs.10 lakh: Fine ONLY (no imprisonment — fully decriminalized)
Previously: "Rigorous imprisonment 6 months to 7 years" — now changed to simple imprisonment.
Second and subsequent offences: Simple imprisonment 6 months to 3 years + fine.
Note: Imprisonment is only for WILFUL evasion — missing a deadline alone never leads to jail.

── HOW TO SAVE TAX (FY 2026-27) ──
Under old regime: 80C Rs.1.5L, 80CCD(1B) NPS Rs.50K, 80D health insurance, HRA, home loan interest.
Under new regime (Section 115BAC): only standard deduction Rs.75K and employer NPS (80CCD(2)).
New regime is highly tax-efficient (zero tax up to Rs.12,75,000 salary) — old regime rarely better.

── FOREIGN ASSETS DISCLOSURE SCHEME 2026 (Finance Act 2026 — Chapter IV) ──
New scheme for disclosure of foreign assets by small taxpayers.
Declarant pays tax + specified amount to get immunity from penalty and prosecution.
Declared assets/income not included in total income for IT Act 1961 or Black Money Act.
Declaration is final — no rectification or set-off claims thereafter.
=================================================================
END OF KNOWLEDGE BASE
=================================================================
"""


# =============================================================================
# LEAN SYSTEM PROMPT
# =============================================================================

SYSTEM_PROMPT = """
You are TaxBot — India's Income Tax AI assistant.

IDENTITY (absolute):
- Name: TaxBot. Never say GPT, Claude, Gemini, Google, OpenAI, or any other model.
- If asked "who are you": say "I am TaxBot, an AI assistant for Indian income tax."
- In Hindi: "main TaxBot hoon — Bharat ka Income Tax AI sahayak."

TAX YEAR CONTEXT (mandatory — always use these, never deviate):
- Current Financial Year: Financial Year two thousand twenty six to twenty seven
- Current Assessment Year: Assessment Year two thousand twenty seven to twenty eight
- Governing law: Income Tax Act, two thousand twenty five (effective April one, two thousand twenty six)
- Finance Act: Finance Act, two thousand twenty six (Finance Bill two thousand twenty six, introduced Feb one, two thousand twenty six)
- New Tax Regime: Section 115BAC (default)
- New regime slabs (Finance Act two thousand twenty six — UNCHANGED): 0-4L:0%, 4-8L:5%, 8-12L:10%, 12-16L:15%, 16-20L:20%, 20-24L:25%, >24L:30%
- ITR deadline for individuals: July thirty one, two thousand twenty seven
- ITR deadline for non-audit business/profession: August thirty one, two thousand twenty seven (NEW)
- Revised return: up to 12 months from end of tax year (fee if filed after 9 months)
- Updated return (ITR-U): 48 months (4 years) from end of financial year succeeding the tax year
- Buyback of shares: taxed as CAPITAL GAINS (NOT dividend) under Income Tax Act two thousand twenty five
- Prosecution: decriminalized for evasion < Rs.10 lakh (fine only); simple imprisonment (not rigorous) for larger amounts
- NEVER write or say "FY 2025-26" or "AY 2026-27" in any response.

SPEECH-FRIENDLY YEAR AND ABBREVIATION FORMATTING (mandatory — applies to every response):
- NEVER write or say "FY" — always write "Financial Year" in full.
- NEVER write or say "AY" — always write "Assessment Year" in full.
- Spell out ALL years in full words:
    2026 → "two thousand twenty six"
    2027 → "two thousand twenty seven"
    2028 → "two thousand twenty eight"
    2032 → "two thousand thirty two"
- Apply to ALL occurrences including hyphenated year ranges:
    "FY 2026-27" → "Financial Year two thousand twenty six to twenty seven"
    "AY 2027-28" → "Assessment Year two thousand twenty seven to twenty eight"
    "Finance Act, 2026" → "Finance Act, two thousand twenty six"
    "April 1, 2026" → "April one, two thousand twenty six"
    "July 31, 2027" → "July thirty one, two thousand twenty seven"
    "March 31, 2028" → "March thirty one, two thousand twenty eight"
    "March 31, 2032" → "March thirty one, two thousand thirty two"
- Exception: Do NOT spell out years inside section numbers, form names, or legal clause references.
  These remain unchanged: "Section 276C", "ITR-2", "80CCD(2)", "Section 115BAC", "234A".

LANGUAGE RULE (highest priority):
- REPLY LANGUAGE = EN → every sentence in English only.
- REPLY LANGUAGE = HI → every sentence in Hindi (Devanagari script) only.
- Hinglish (Roman Hindi like "bhai", "yaar", "batao") → Hindi.
- Never mix languages.

ROUTING:
- INCOME TAX QUERY → answer from the KNOWLEDGE BASE provided in the user message.
- GENERAL QUERY → answer from web search results in CONTEXT, or own knowledge.

NUMBER FORMAT (mandatory):
- ALL rupee amounts: Indian comma format: Rs.12,00,000 not Rs.1200000.
- NEVER abbreviate lakh as "L". Use "lakh" in full.

CALCULATION FORMAT:
- Use "giving" not "=" in tax step-by-step: "5% on Rs.4,00,000 giving Rs.20,000"
- Pre-computed numbers provided — use ONLY those exact figures.

URL: Always write: https://www.incometax.gov.in

HINDI NUMBER FORMAT: Write rupee amounts in Hindi words: Rs.20,000 → bees hazaar rupaye.

FORMAT:
- Answer first — no preamble.
- Plain prose only. No markdown tables. No pipe characters (|).
- Max 3-4 lines for any summary.
- No filler phrases.
""".strip()


# =============================================================================
# KB TOPIC INDEX
# =============================================================================

_KB_SECTIONS: dict[str, str] = {}


def _build_kb_index() -> None:
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

_TOPIC_SECTIONS: list[tuple[set[str], str]] = [
    ({"slab", "rate", "regime", "new regime", "old regime", "87a", "rebate",
      "surcharge", "cess", "standard deduction", "tax rate", "percentage",
      "how much tax", "kitna tax", "\u0915\u093f\u0924\u0928\u093e \u091f\u0948\u0915\u094d\u0938", "115bac"},
     "NEW TAX REGIME — Section 115BAC (DEFAULT, no action needed)"),
    ({"80c", "80d", "80e", "80g", "80gg", "80u", "80dd", "80ddb", "80tta",
      "80ttb", "80eea", "deduction", "ppf", "elss", "nps", "lic", "insurance",
      "home loan", "section 24", "\u0915\u091f\u094c\u0924\u0940"},
     "WHAT IS AVAILABLE IN NEW vs OLD REGIME"),
    ({"itr", "file", "filing", "form", "sahaj", "sugam", "return", "deadline",
      "due date", "belated", "revised", "updated", "e-verify", "itr-u",
      "itr-1", "itr-2", "itr-3", "itr-4", "\u0930\u093f\u091f\u0930\u094d\u0928", "\u0926\u093e\u0916\u093f\u0932"},
     "ITR FILING (FY 2026-27 / AY 2027-28) — Finance Act 2026"),
    ({"capital gain", "ltcg", "stcg", "section 54", "54ec", "54f", "shares",
      "equity", "mutual fund", "property sold", "holding period",
      "indexation", "cgas", "buyback", "buy back", "\u092a\u0942\u0902\u091c\u0940\u0917\u0924 \u0932\u093e\u092d"},
     "CAPITAL GAINS (FY 2026-27)"),
    ({"tds", "form 16", "form 16a", "26as", "ais", "15g", "15h", "tds rate",
      "deducted", "\u0938\u094d\u0930\u094b\u0924 \u092a\u0930 \u0915\u0930"}, "TDS"),
    ({"tcs", "foreign remittance", "lrs", "overseas", "tour package"},
     "TCS (FY 2026-27 RATES)"),
    ({"advance tax", "234a", "234b", "234c", "self assessment", "challan 280",
      "instalment", "\u0905\u0917\u094d\u0930\u093f\u092e \u0915\u0930"}, "ADVANCE TAX (FY 2026-27)"),
    ({"senior citizen", "80 years", "194p", "\u0935\u0930\u093f\u0937\u094d\u0920 \u0928\u093e\u0917\u0930\u093f\u0915"},
     "SENIOR CITIZENS (FY 2026-27)"),
    ({"penalty", "prosecution", "jail", "fine", "imprisonment", "decriminal",
      "276c", "276b", "evasion", "\u091c\u0941\u0930\u094d\u092e\u093e\u0928\u093e"},
     "PROSECUTION — DECRIMINALIZED (Finance Act 2026)"),
    ({"notice", "scrutiny", "143", "148", "appeal", "cit", "itat", "din", "\u0928\u094b\u091f\u093f\u0938"},
     "NOTICES"),
    ({"gratuity", "leave encashment", "vrs", "\u0917\u094d\u0930\u0947\u091a\u094d\u092f\u0941\u091f\u0940"}, "GRATUITY"),
    ({"salary", "\u0935\u0947\u0924\u0928", "salary calculation", "my salary"},
     "COMMON SALARY SCENARIOS (New Regime / Section 115BAC, salaried, FY 2026-27)"),
    ({"save tax", "tax saving", "how to save", "\u092c\u091a\u0924"},
     "HOW TO SAVE TAX (FY 2026-27)"),
    ({"foreign asset", "disclosure scheme", "undisclosed", "black money"},
     "FOREIGN ASSETS DISCLOSURE SCHEME 2026 (Finance Act 2026 — Chapter IV)"),
]


def _get_relevant_kb(query: str) -> str:
    q = query.lower()
    matched_sections: list[str] = []
    seen: set[str] = set()
    for keywords, section_name in _TOPIC_SECTIONS:
        if any(kw in q for kw in keywords) and section_name not in seen:
            section_text = _KB_SECTIONS.get(section_name, "")
            if section_text:
                matched_sections.append(f"[{section_name}]\n{section_text}")
                seen.add(section_name)

    if matched_sections:
        return "\n\n".join(matched_sections)

    defaults = [
        "NEW TAX REGIME — Section 115BAC (DEFAULT, no action needed)",
        "OLD TAX REGIME (must explicitly choose)",
        "COMMON SALARY SCENARIOS (New Regime / Section 115BAC, salaried, FY 2026-27)",
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

        detected = detect_language(query)
        if detected != "en":
            language_hint = detected
        lang_label = _LANGUAGE_NAMES.get(language_hint, _LANGUAGE_NAMES["en"])

        is_tax = _is_income_tax_query(query)

        calc_block = ""
        if is_tax and _is_tax_calc_query(query):
            salary = _extract_salary_amount(query)
            if salary and 10_000 < salary < 100_000_000:
                deductions = _extract_deductions(query)
                calc_block = _build_tax_calc_context(salary, deductions)
                logger.info("Tax calc injected: salary=%d deductions=%d", salary, deductions)

        if is_tax:
            query_type_label = "INCOME TAX QUERY"
            if calc_block:
                context_instruction = (
                    "A PRE-COMPUTED TAX CALCULATION for Financial Year two thousand twenty six "
                    "to twenty seven, Assessment Year two thousand twenty seven to twenty eight "
                    "is provided. Use EXACTLY those numbers. Show step-by-step for BOTH regimes. "
                    "State winner clearly."
                )
                relevant_kb = _get_relevant_kb(query)
                full_context = (
                    f"{calc_block}\n\n"
                    f"[RELEVANT KNOWLEDGE BASE — Financial Year two thousand twenty six to "
                    f"twenty seven / Finance Act two thousand twenty six]\n{relevant_kb}"
                )
            else:
                context_instruction = (
                    "Answer from the KNOWLEDGE BASE below. "
                    "All information is for Financial Year two thousand twenty six to twenty seven, "
                    "Assessment Year two thousand twenty seven to twenty eight "
                    "under Finance Act two thousand twenty six."
                )
                full_context = (
                    f"[KNOWLEDGE BASE — Financial Year two thousand twenty six to twenty seven / "
                    f"Assessment Year two thousand twenty seven to twenty eight / "
                    f"Finance Act two thousand twenty six]\n"
                    f"{_get_relevant_kb(query)}"
                )
        else:
            query_type_label = "GENERAL QUERY — WEB SEARCH RESULTS"
            context_instruction = (
                "Answer from the web search results in CONTEXT. "
                "If empty, use your own knowledge. Do NOT refuse."
            )
            full_context = context or ""

        identity_keywords = [
            "who are you", "what are you", "which ai", "which model",
            "which llm", "are you gpt", "are you gemini", "are you claude",
            "are you chatgpt", "who made you", "which company",
            "aap kaun", "aap kya", "tum kaun", "main kaun", "kon ho tum",
            "\u0906\u092a \u0915\u094c\u0928", "\u0924\u0941\u092e \u0915\u094c\u0928",
            "\u0915\u094c\u0928 \u0938\u0947", "\u0915\u093f\u0938 \u0915\u0902\u092a\u0928\u0940",
        ]
        is_identity_query = any(kw in query.lower() for kw in identity_keywords)
        identity_note = (
            "\n- IDENTITY LOCK: You MUST answer: 'I am TaxBot, an AI assistant for Indian income tax.' "
            "Do NOT mention Google, OpenAI, GPT, Claude, Gemini, or any other company/model."
        ) if is_identity_query else ""

        user_content = (
            f"REPLY LANGUAGE: {language_hint.upper()}\n"
            f"The user wrote in {lang_label}. Your ENTIRE response must be in that language only.\n\n"
            f"QUERY TYPE: {query_type_label}\n"
            f"TAX YEAR: Financial Year two thousand twenty six to twenty seven | "
            f"Assessment Year two thousand twenty seven to twenty eight | "
            f"Income Tax Act, two thousand twenty five | "
            f"Finance Act, two thousand twenty six\n\n"
            f"--- CONTEXT ---\n{full_context}\n--- END CONTEXT ---\n\n"
            f"User Question: {query}\n\n"
            f"- {context_instruction}\n"
            f"- Language is {language_hint.upper()}. Every sentence must be in {lang_label}."
            f"{identity_note}\n"
            "- Answer directly. No XML. No tables. No pipes (|). No meta-commentary."
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