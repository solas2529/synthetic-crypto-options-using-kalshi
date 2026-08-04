"""Build a vanilla options chain out of Kalshi binary (digital) markets.

A Kalshi market that pays $1 if BTC settles above K is a cash-or-nothing digital
call.  Under the settlement measure its price *is* the survival probability

    D(K) = Q(S_T > K)

and vanilla options are the Riemann integral of that curve:

    C(K) = E[(S_T - K)+] = int_K^inf  Q(S_T > u) du
    P(K) = E[(K - S_T)+] = int_0^K    Q(S_T <= u) du
    F    = E[S_T]        = int_0^inf  Q(S_T > u) du

So a ladder of Kalshi strikes already *is* a discretised call chain -- you only
have to sum it.  This module does that sum, plus the plumbing you need for the
result to be trustworthy: quote normalisation, arbitrage repair, tail handling,
and a Black-76 inversion to get implied vols back out.

The library half touches no network and needs no third-party packages; feed it
dicts from /trade-api/v2/markets.  The front end at the bottom imports
`kalshi_client` lazily, so `import riemann_chain` still works without
requests/cryptography.

    python3 riemann_chain.py

is the whole interface: it asks which coin, lists that coin's open expiries in
your local timezone, and on your pick writes the chain table plus the call and
put payoff figures for the strike nearest the forward.  The offline synthetic
round-trip suite is the last entry on the coin menu.
"""

from __future__ import annotations

import math
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal, Sequence

__all__ = [
    "Digital",
    "ChainRow",
    "Greeks",
    "OptionChain",
    "build_chain",
    "digitals_from_markets",
    "selftest",
    "main",
]

Side = Literal["bid", "mid", "ask"]


# --------------------------------------------------------------------------
# normal distribution helpers (no numpy/scipy dependency)
# --------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _xml_escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# --------------------------------------------------------------------------
# SVG helpers, shared by both figures
# --------------------------------------------------------------------------
# Light-mode ink, written as presentation attributes; the dark values are
# layered on in CSS so a rasteriser that ignores <style> still renders.
_INK = {"ink": "#0b0b0b", "ink2": "#52514e", "mut": "#898781",
        "acc": "#2a78d6", "acc2": "#eb6834"}

# CDF window the printed table and the figures' value blocks keep. Strikes
# outside it are quoted but carry almost no mass, and listing them buries the
# rows that matter under a wall of zeroes.
_TABLE_BAND = 0.02

_MONO = "ui-monospace,'SF Mono',Menlo,Consolas,monospace"
_SANS = "ui-sans-serif,system-ui,-apple-system,'Segoe UI',Helvetica,sans-serif"


def _text(x, y, s, cls, anchor="start", size=12.5, mono=True, weight=400) -> str:
    return (f'<text class="{cls}" x="{x:.0f}" y="{y:.0f}" fill="{_INK[cls]}" '
            f'font-family="{_MONO if mono else _SANS}" font-size="{size}" '
            f'font-weight="{weight}" text-anchor="{anchor}">{_xml_escape(s)}</text>')


def _pts(points: Iterable[tuple[float, float]]) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def _value_table(chain, kind: Literal["call", "put"], pad: int, w: int,
                 y0: float) -> tuple[list[str], float]:
    """Per-strike values and greeks, drawn under the payoff panels.

    The panels above argue about one strike; this says what every other rung is
    worth.  Same numbers as the printed table and chain.svg -- the quote, the
    implied distribution, the reconstructed option, its vol -- with the greeks
    alongside, so the figure carries the whole chain rather than a headline.

    Banded to `_TABLE_BAND` like the printed table, because the panels get the
    *full* ladder (that is where the 'quotes stop here' line comes from) and a
    live ladder can run hundreds of strikes whose rows would be all zeroes.
    """
    rows = [r for r in chain.rows if _TABLE_BAND <= r.cdf <= 1.0 - _TABLE_BAND]
    hidden = len(chain.rows) - len(rows)
    if not rows:                       # every strike is a wing; show them anyway
        rows, hidden = list(chain.rows), 0
    if not rows:
        return [], 0.0

    v = "C" if kind == "call" else "P"
    cols = [  # key, label, width
        ("strike", "strike", 84), ("dk", "D(K)", 60), ("cdf", "cdf", 58),
        ("pdf", "pdf x1e6", 78), ("val", kind, 82), ("iv", "iv", 60),
        ("delta", "delta", 68), ("gamma", "gamma x1e6", 84),
        ("vega", "vega", 62), ("theta", "theta", 70), ("dual", f"d{v}/dK", 74),
    ]
    gut, rh, hh = 26, 22, 24
    xs, run = {}, pad + gut
    for k, _l, cw in cols:
        xs[k] = (run, cw)
        run += cw

    atm = chain.atm()
    o = [_text(pad, y0 + 12, f"every quoted strike in the "
               f"{_TABLE_BAND:.0%}-{1 - _TABLE_BAND:.0%} CDF band", "ink2",
               size=11, mono=False, weight=600)]
    hy = y0 + 34
    for k, label, cw in cols:
        x0, _ = xs[k]
        o.append(_text(x0 + cw - 10, hy, label, "mut", anchor="end",
                       size=10, mono=False))
    o.append(f'<line class="ax" x1="{pad}" y1="{hy + 8:.0f}" x2="{w - pad}" '
             f'y2="{hy + 8:.0f}" stroke="#c3c2b7" stroke-width="1"/>')

    body = hy + 8
    for i, r in enumerate(rows):
        g = chain.greeks(r, kind)
        y = body + rh * i
        base = y + rh - 7
        if atm is not None and r.strike == atm.strike:
            o.append(f'<rect class="hl" x="{pad}" y="{y:.0f}" width="{w - 2*pad}" '
                     f'height="{rh}" rx="4" fill="#2a78d6" opacity=".07"/>')
            o.append(f'<rect class="acc" x="{pad}" y="{y+3:.0f}" width="3" '
                     f'height="{rh-6}" rx="1.5" fill="#2a78d6"/>')
            o.append(_text(pad + 9, base, "ATM", "acc", size=8, mono=False, weight=600))
        elif i:
            o.append(f'<line class="rl" x1="{pad+gut}" y1="{y:.0f}" x2="{w-pad}" '
                     f'y2="{y:.0f}" stroke="#e1e0d9" stroke-width="1"/>')

        dash = "--"
        vals = {
            "strike": f"{r.strike:,.0f}",
            "dk": f"{1.0 - r.cdf:.3f}",
            "cdf": f"{r.cdf:.3f}",
            "pdf": f"{r.pdf * 1e6:,.0f}",
            "val": f"{(r.call if kind == 'call' else r.put):,.2f}"
                   + ("*" if r.model_dependent else ""),
            "iv": f"{r.iv * 100:.1f}%" if r.iv is not None else dash,
            "delta": dash if g.delta is None else f"{g.delta:.3f}",
            "gamma": dash if g.gamma is None else f"{g.gamma * 1e6:,.1f}",
            "vega": dash if g.vega is None else f"{g.vega:,.2f}",
            "theta": dash if g.theta is None else f"{g.theta:,.1f}",
            "dual": f"{g.dual_delta:.3f}",
        }
        for k, _l, cw in cols:
            x0, _ = xs[k]
            # the strike derivative is the one column the ladder quotes outright,
            # so it carries the digitals' blue here exactly as it does in the strip
            cls = "acc" if k == "dual" else ("ink" if k in ("strike", "val") else "ink2")
            o.append(_text(x0 + cw - 10, base, vals[k], cls, anchor="end",
                           size=11.5, weight=600 if k == "strike" else 400))

    h = (body + rh * len(rows)) - y0 + 6
    if hidden:
        o.append(_text(pad, y0 + h + 6, f"({hidden} strike"
                       f"{'' if hidden == 1 else 's'} outside the band hidden; the "
                       f"panels above still use the full ladder)", "mut",
                       size=10, mono=False))
        h += 18
    return o, h


def _greek_cells(g) -> list[tuple[str, str, bool]]:
    """(label, value, is_exact) per greek, for the strip both figures draw.

    The strike derivative is listed last but is the only one on the row that the
    ladder actually knows; `is_exact` is what the figures grey the others down
    by, so the picture never implies the Black-76 four are quoted.
    """
    v = "C" if g.kind == "call" else "P"
    dash = "--"
    return [
        (f"delta  d{v}/dF", dash if g.delta is None else f"{g.delta:.4f}", False),
        ("gamma  x1e6", dash if g.gamma is None else f"{g.gamma * 1e6:,.1f}", False),
        ("vega  per vol pt", dash if g.vega is None else f"{g.vega:,.2f}", False),
        ("theta  per day", dash if g.theta is None else f"{g.theta:,.2f}", False),
        (f"d{v}/dK  off the ladder", f"{g.dual_delta:.4f}", True),
    ]


def _wrap(lines: Sequence[str], width: int = 104) -> list[str]:
    out: list[str] = []
    for f in lines:
        line = ""
        for word in f.split():
            if len(line) + len(word) + 1 > width:
                out.append(line)
                line = "   " + word
            else:
                line = f"{line} {word}".strip()
        out.append(line)
    return out


def _stack_stub(rule: str, why: Sequence[str], event: str = "") -> str:
    """The honest fallback when the ladder cannot carry a replicating stack.

    Drawing a one-step "stack" would look like a picture and mean nothing, so the
    payoff figures bail instead -- but bailing with only the rule quoted back at
    you is no help either.  `why` carries the shape of the ladder that failed it,
    since on these series the usual cause is not a bug but a book: strikes
    resting on the extreme tick are dropped by `_pinned`, and on a short-dated
    crypto expiry that routinely leaves three or four live rungs out of two
    hundred.
    """
    w, pad = 880, 26
    lines = _wrap([rule, *why], width=110)
    h = 56 + 20 * len(lines)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{w}" height="{h}" role="img" aria-label="{_xml_escape(rule)}'
        f'{" for " + _xml_escape(event) if event else ""}">',
        '<style>svg{color-scheme:light dark}.p{fill:#f9f9f7}.ink2{fill:#52514e}'
        '.mut{fill:#898781}@media(prefers-color-scheme:dark){.p{fill:#0d0d0d}'
        '.ink2{fill:#c3c2b7}.mut{fill:#898781}}</style>',
        f'<rect class="p" width="{w}" height="{h}" fill="#f9f9f7"/>',
    ]
    for i, line in enumerate(lines):
        out.append(_text(pad, 36 + 20 * i, line, "ink2" if i == 0 else "mut",
                         size=12, mono=False))
    out.append("</svg>")
    return "\n".join(out)


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Digital:
    """One binary market, normalised to a *digital call* on `strike`.

    bid/ask/mid are probabilities in [0, 1] for the event {S_T > strike}.
    A market quoted as "below K" is flipped into this convention on the way in,
    which also swaps the sides (1 - ask becomes the bid).
    """

    strike: float
    bid: float
    ask: float
    ticker: str = ""
    mid_override: float | None = None

    @property
    def mid(self) -> float:
        # Bin-derived levels set this explicitly: cumulating bid and ask sides
        # separately lets a sum run past 1 and get clamped, which would drag the
        # midpoint off the (well-behaved) cumulated mid.
        if self.mid_override is not None:
            return self.mid_override
        return 0.5 * (self.bid + self.ask)

    def price(self, side: Side) -> float:
        return {"bid": self.bid, "mid": self.mid, "ask": self.ask}[side]


@dataclass(frozen=True)
class ChainRow:
    """One strike of the reconstructed vanilla chain."""

    strike: float
    digital_bid: float
    digital_mid: float
    digital_ask: float
    cdf: float           # Q(S_T <= K)
    pdf: float           # risk-neutral density, per $1 of strike
    call: float          # trapezoid + modelled tail: the point estimate
    call_lo: float       # right-Riemann, tail dropped -> rigorous lower bound
    call_hi: float       # left-Riemann + modelled tail -> upper *estimate*
    put: float           # by put-call parity off the same curve
    iv: float | None     # Black-76 implied vol from `call`, None if inversion fails
    tail_weight: float = 0.0  # fraction of `call` coming from the extrapolated tail
    ticker: str = ""

    @property
    def call_spread_width(self) -> float:
        """Width of the [lo, hi] band -- what the strike grid plus the tail cost you."""
        return self.call_hi - self.call_lo

    @property
    def model_dependent(self) -> bool:
        """True when the extrapolated tail, not quoted strikes, drives this row."""
        return self.tail_weight > 0.25


@dataclass(frozen=True)
class Greeks:
    """Sensitivities of one reconstructed vanilla, in two tiers.

    `dual_delta` and `dual_gamma` are *exact*.  Differentiating
    C(K) = DF int_K^inf Q(S > u) du in the strike gives back the integrand,

        dC/dK   = -DF Q(S_T > K)      d2C/dK2 = DF f(K)

    which is Breeden-Litzenberger read backwards: the two strike derivatives are
    the digital quote and the density, both of which the ladder already carries.
    No model, no vol, nothing fitted -- they are the `digital_mid` and `pdf`
    columns wearing different units, and they stay right even where the smile
    does not.

    `delta`, `gamma`, `vega` and `theta` differentiate in the forward, the vol
    and the clock, none of which a single expiry's strike ladder pins down: the
    quotes fix the distribution of S_T, not how it responds when F or sigma
    moves.  So these are Black-76 at this row's own implied vol -- a smile-local
    reading, exact only for the option it was struck from, and unavailable at all
    (None) when the inversion fails.

    Units are the ones a trader quotes, not the raw partials -- see the field
    comments.  `parity` is what a call and put on the same strike must satisfy.
    """

    delta: float | None       # dC/dF, per $1 of forward
    gamma: float | None       # d2C/dF2, per $1^2 -- displayed x 1e6
    vega: float | None        # dC/dsigma, per *vol point* (partial / 100)
    theta: float | None       # -dC/dt, per *day* (partial / 365)
    dual_delta: float         # dC/dK, exact: -DF x the digital quote
    dual_gamma: float         # d2C/dK2, exact: DF x the density
    kind: Literal["call", "put"] = "call"


@dataclass
class OptionChain:
    forward: float               # E[S_T] implied by the binaries
    expiry: datetime | None
    tau: float                   # years to expiry
    discount: float              # exp(-r * tau)
    rows: list[ChainRow] = field(default_factory=list)
    truncated_mass: float = 0.0  # prob mass outside the quoted strike ladder
    warnings: list[str] = field(default_factory=list)

    @property
    def strikes(self) -> list[float]:
        return [r.strike for r in self.rows]

    def atm(self) -> ChainRow | None:
        """Row whose strike is closest to the implied forward."""
        if not self.rows:
            return None
        return min(self.rows, key=lambda r: abs(r.strike - self.forward))

    def atm_iv(self) -> float | None:
        row = self.atm()
        return row.iv if row else None

    def greeks(self, row: ChainRow | None = None,
               kind: Literal["call", "put"] = "call") -> Greeks | None:
        """Sensitivities for one row, defaulting to the ATM strike.

        The strike derivatives come off the ladder directly; the rest are
        Black-76 at `row.iv`, and are None when that inversion failed.  See
        `Greeks` for why the two tiers are not interchangeable.
        """
        row = row if row is not None else self.atm()
        if row is None:
            return None

        # Exact, straight off the quotes.  A call's value falls as the strike
        # rises, at exactly the rate the digital there is worth; a put's climbs
        # at the rate of the complementary digital.
        surv = max(0.0, min(1.0, 1.0 - row.cdf))
        dual_delta = self.discount * (-surv if kind == "call" else 1.0 - surv)
        dual_gamma = self.discount * row.pdf

        if row.iv is None or self.tau <= 0 or self.forward <= 0:
            return Greeks(None, None, None, None, dual_delta, dual_gamma, kind)

        delta, gamma, vega, theta = _black76_greeks(
            self.forward, row.strike, self.tau, row.iv, self.discount, kind)
        return Greeks(delta, gamma, vega, theta, dual_delta, dual_gamma, kind)

    def replicable(self) -> tuple[bool, bool]:
        """(call figure drawable, put figure drawable).

        A stack needs at least two rungs on its own side of the money -- above K
        for the call, below K for the put -- or there is nothing to draw but a
        single step masquerading as a replication.  The payoff figures and the
        front end share this so neither has to re-derive the rule.
        """
        n = len(self.rows)
        atm = self.atm()
        ai = self.rows.index(atm) if atm is not None else -1
        return (n >= 3 and 0 <= ai <= n - 3, n >= 3 and ai >= 2)

    def _ladder_shape(self, ai: int) -> list[str]:
        """Why a payoff figure had nothing to draw, in terms of this ladder.

        Almost always the answer is the book rather than the code: `_pinned`
        drops the strikes resting on the extreme tick, and on a short-dated
        crypto expiry the whole distribution can live inside three or four rungs
        of a two-hundred-strike ladder, leaving nothing to stack.  So quote the
        counts back, and carry the drop warning that explains them.
        """
        n = len(self.rows)
        if not n:
            return ["no strike survived quote normalisation: every book on this "
                    "event was empty, crossed, or resting on the extreme tick"]
        ks = self.strikes
        where = (f"the money sits on rung {ai + 1} of {n}, with {ai} live "
                 f"{'strike' if ai == 1 else 'strikes'} below it and "
                 f"{n - ai - 1} above" if ai >= 0 else
                 f"{n} live {'strike' if n == 1 else 'strikes'}, none at the money")
        out = [f"{n} live {'strike' if n == 1 else 'strikes'} "
               f"({ks[0]:,.0f} to {ks[-1]:,.0f}); {where}."]
        dropped = [w for w in self.warnings if "pinned at the extreme tick" in w]
        if dropped:
            out.append(dropped[0])
            out.append("that is the book, not the reconstruction: a nearer expiry "
                       "quotes fewer live rungs. The chain table and chain.svg are "
                       "still valid -- try a longer-dated expiry for the figures.")
        return out

    def format_table(self) -> str:
        head = (
            f"{'strike':>12} {'bid':>6} {'mid':>6} {'ask':>6} "
            f"{'cdf':>6} {'pdf x1e6':>9} {'call':>10} {'+/-':>8} {'put':>10} {'iv':>7}"
        )
        lines = [head, "-" * len(head)]
        for r in self.rows:
            iv = f"{r.iv * 100:6.1f}%" if r.iv is not None else "     --"
            lines.append(
                f"{r.strike:>12,.0f} {r.digital_bid:>6.3f} {r.digital_mid:>6.3f} "
                f"{r.digital_ask:>6.3f} {r.cdf:>6.3f} {r.pdf * 1e6:>9.3f} "
                f"{r.call:>10,.2f} {r.call_spread_width:>8,.2f} {r.put:>10,.2f} {iv:>7}"
                f"{'  *' if r.model_dependent else ''}"
            )
        if any(r.model_dependent for r in self.rows):
            lines.append("  * value driven by the extrapolated tail, not by quoted strikes")
        return "\n".join(lines)

    def format_svg(self, event: str = "") -> str:
        """Render the chain as a standalone SVG table.

        Same columns as `format_table`, plus an inline density bar. Numerals are
        monospaced so the columns align in any renderer, and the light palette is
        written as presentation attributes with the themed values layered on in
        CSS -- so a browser gets dark mode and a dumb rasteriser still gets a
        correct light render instead of black-on-black.
        """
        cols = [  # key, label, width, align ('e' right, 's' start)
            ("strike", "strike",  82, "e"), ("bid", "bid", 54, "e"),
            ("mid",    "mid",     54, "e"), ("ask", "ask", 54, "e"),
            ("cdf",    "cdf",     58, "e"), ("pdf", "pdf x1e6", 68, "e"),
            ("bar",    "density",112, "s"), ("call", "call", 78, "e"),
            ("band",   "band",    64, "e"), ("put",  "put",  78, "e"),
            ("iv",     "iv",      62, "e"),
        ]
        pad, gut, rh, hh = 26, 26, 27, 32
        w = pad * 2 + gut + sum(c[2] for c in cols)
        top = 132                                  # header block
        xs, run = {}, pad + gut
        for k, _l, cw, _a in cols:
            xs[k] = (run, cw)
            run += cw

        foot = []
        if any(r.model_dependent for r in self.rows):
            foot.append("*  value driven by the extrapolated tail, not by quoted strikes")
        foot.append("band = call_hi - call_lo, the cost of the strike grid plus the tail")
        foot += [f"warning:  {x}" for x in self.warnings]
        wrapped = _wrap(foot)
        h = top + hh + rh * max(len(self.rows), 1) + 18 + 17 * len(wrapped) + pad

        pmax = max((r.pdf for r in self.rows), default=0.0) or 1.0
        atm = self.atm()
        e = _xml_escape
        txt = _text

        o: list[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h:.0f}" '
            f'width="{w}" height="{h:.0f}" role="img" '
            f'aria-label="Reconstructed option chain{" for " + e(event) if event else ""}">',
            "<style>",
            # Declaring both schemes stops Chromium's auto-dark filter from
            # "helpfully" inverting the ink and leaving the surfaces light.
            "svg{color-scheme:light dark}",
            ".p{fill:#f9f9f7}.c{fill:#fcfcfb}.ink{fill:#0b0b0b}.ink2{fill:#52514e}",
            ".mut{fill:#898781}.acc{fill:#2a78d6}.bar{fill:#2a78d6}.trk{fill:#e1e0d9}",
            ".rl{stroke:#e1e0d9}.ax{stroke:#c3c2b7}.hl{fill:#2a78d6;opacity:.07}",
            ".rg{stroke:rgba(11,11,11,.10)}",
            "@media(prefers-color-scheme:dark){",
            ".p{fill:#0d0d0d}.c{fill:#1a1a19}.ink{fill:#fff}.ink2{fill:#c3c2b7}",
            ".mut{fill:#898781}.acc{fill:#3987e5}.bar{fill:#3987e5}.trk{fill:#2c2c2a}",
            ".rl{stroke:#2c2c2a}.ax{stroke:#383835}.hl{fill:#3987e5;opacity:.12}",
            ".rg{stroke:rgba(255,255,255,.10)}}",
            "</style>",
            f'<rect class="p" width="{w}" height="{h:.0f}" fill="#f9f9f7"/>',
            f'<rect class="c rg" x="{pad/2:.0f}" y="{pad/2:.0f}" width="{w-pad:.0f}" '
            f'height="{h-pad:.0f}" rx="10" fill="#fcfcfb" stroke="rgba(11,11,11,.10)"/>',
        ]

        # ---- header block -------------------------------------------------
        o.append(txt(pad, 52, event or "option chain", "ink", size=17, mono=False, weight=600))
        sub = f"{len(self.rows)} strikes reconstructed from Kalshi binaries"
        if self.expiry:
            sub += f"   ·   expiry {self.expiry:%Y-%m-%d %H:%M} UTC   ·   tau {self.tau*365*24:.2f}h"
        o.append(txt(pad, 71, sub, "mut", size=11.5, mono=False))

        stats = [("forward", f"{self.forward:,.2f}")]
        if atm and atm.iv:
            stats.append(("ATM IV", f"{atm.iv*100:.1f}%"))
        stats.append(("tail mass", f"{self.truncated_mass:.2%}"))
        sx = pad
        for label, val in stats:
            o.append(txt(sx, 99, label, "mut", size=10.5, mono=False))
            o.append(txt(sx, 121, val, "ink", size=19, weight=600))
            sx += max(len(val) * 12 + 34, 122)

        # ---- column headers ------------------------------------------------
        hy = top + 20
        for k, label, cw, align in cols:
            x0, _ = xs[k]
            o.append(txt(x0 + (cw - 10 if align == "e" else 0), hy, label, "mut",
                         anchor="end" if align == "e" else "start", size=10.5, mono=False))
        o.append(f'<line class="ax" x1="{pad}" y1="{top+hh-6}" x2="{w-pad}" '
                 f'y2="{top+hh-6}" stroke="#c3c2b7" stroke-width="1"/>')

        # ---- rows ------------------------------------------------------------
        for i, r in enumerate(self.rows):
            y = top + hh + rh * i
            base = y + rh - 9
            if atm is not None and r.strike == atm.strike:
                o.append(f'<rect class="hl" x="{pad}" y="{y}" width="{w-2*pad}" '
                         f'height="{rh}" rx="4" fill="#2a78d6" opacity=".07"/>')
                o.append(f'<rect class="acc" x="{pad}" y="{y+4}" width="3" '
                         f'height="{rh-8}" rx="1.5" fill="#2a78d6"/>')
                o.append(txt(pad + 9, base, "ATM", "acc", size=8.5, mono=False, weight=600))
            elif i:
                o.append(f'<line class="rl" x1="{pad+gut}" y1="{y}" x2="{w-pad}" y2="{y}" '
                         f'stroke="#e1e0d9" stroke-width="1"/>')

            vals = {
                "strike": f"{r.strike:,.0f}", "bid": f"{r.digital_bid:.3f}",
                "mid": f"{r.digital_mid:.3f}", "ask": f"{r.digital_ask:.3f}",
                "cdf": f"{r.cdf:.3f}", "pdf": f"{r.pdf*1e6:,.0f}",
                "call": f"{r.call:,.2f}", "band": f"{r.call_spread_width:,.2f}",
                "put": f"{r.put:,.2f}",
                "iv": (f"{r.iv*100:.1f}%" if r.iv is not None else "--")
                      + ("*" if r.model_dependent else ""),
            }
            for k, _l, cw, align in cols:
                if k == "bar":
                    bx, _ = xs[k]
                    bw = max((r.pdf / pmax) * (cw - 14), 0.0)
                    o.append(f'<rect class="trk" x="{bx}" y="{base-7}" width="{cw-14}" '
                             f'height="6" rx="3" fill="#e1e0d9" opacity=".55"/>')
                    if bw > 0.5:
                        o.append(f'<rect class="bar" x="{bx}" y="{base-7}" '
                                 f'width="{bw:.1f}" height="6" rx="3" fill="#2a78d6"/>')
                    continue
                x0, _ = xs[k]
                heavy = k in ("call", "put", "strike")
                o.append(txt(x0 + cw - 10, base, vals[k], "ink" if heavy else "ink2",
                             anchor="end", size=12.5, weight=600 if k == "strike" else 400))

        # ---- footnotes ---------------------------------------------------------
        fy = top + hh + rh * len(self.rows) + 26
        for line in wrapped:
            o.append(txt(pad, fy, line, "mut", size=10.5, mono=False))
            fy += 17

        o.append("</svg>")
        return "\n".join(o)

    def format_payoff_svg(self, event: str = "") -> str:
        """Draw the ATM call twice: once as an area, once as a payoff.

        Left panel is the Riemann picture the module is named for.  D(K) with the
        region right of the ATM strike shaded -- that area *is* C(K_atm) -- and
        the two staircases straddling the curve are the left- and right-hand
        sums, which is to say `call_hi` and `call_lo`.

        Right panel is those same two sums read at expiry instead of at the
        integral.  Holding dK_i digitals struck at K_i, for every K_i at or above
        the strike, super-replicates (S_T - K)+; holding the same widths one
        strike up sub-replicates it.  The true hockey stick is trapped between,
        so the vertical gap here and the chain's `band` column are the same
        quantity seen from two directions -- and both close as dK -> 0.

        Past the top quoted strike the stacks go flat while the payoff keeps
        climbing.  That wedge is what the tail model is guessing at, and it is
        why `call_lo` is a bound and `call_hi` is only an estimate.
        """
        e = _xml_escape
        pad, gap = 26, 34
        w = 880
        top = 190                                # header block: two stat rows
        ax_l, ax_b, ph, th = 54, 32, 250, 26     # y gutter, x gutter, plot h, panel title
        pw = (w - 2 * pad - gap) / 2
        piw = pw - ax_l
        py0 = top + th
        py1 = py0 + ph
        pxa = pad + ax_l                         # panel A plot origin
        pxb = pad + pw + gap + ax_l              # panel B plot origin

        rows = self.rows
        atm = self.atm()
        ai = rows.index(atm) if atm is not None else -1
        # Below three strikes, or with the money at the top of the ladder, there
        # is no stack to draw -- say so rather than render a misleading picture.
        if not self.replicable()[0]:
            return _stack_stub(
                "no call figure: the call stack is held across the strikes at and "
                "above K, and this ladder does not have two of them",
                self._ladder_shape(ai), event)

        ks = [r.strike for r in rows]
        surv = [1.0 - r.cdf for r in rows]
        katm = atm.strike
        kmin, ktop = ks[0], ks[-1]

        # The call at the top strike is pure tail, so it hands back both the tail
        # area and -- divided by the survival level there -- the decay length the
        # tail model fitted.  Draw ~3 of those, capped so the quoted ladder keeps
        # most of the panel.
        tail_area = rows[-1].call / self.discount if self.discount else 0.0
        s_last = surv[-1]
        lam = tail_area / s_last if s_last > 1e-9 and tail_area > 0 else 0.0
        tail_ext = min(3.0 * lam, 0.45 * (ktop - kmin)) if lam > 0 else 0.0
        kend = ktop + tail_ext
        span = kend - kmin or 1.0

        ymax = (kend - katm) * 1.06 or 1.0

        def X(px: float, k: float) -> float:
            return px + (k - kmin) / span * piw

        def YA(d: float) -> float:                    # panel A: probability
            return py1 - max(0.0, min(1.0, d)) * ph

        def YB(v: float) -> float:                    # panel B: dollars of payoff
            return py1 - max(0.0, min(ymax, v)) / ymax * ph

        foot = [
            "left: the shaded area is C(K_atm) = int_K^inf D(u) du, ruled at every strike into "
            "the dK columns it is summed from. The staircase above the curve is the left-hand "
            "sum (call_hi), the one below it the right-hand sum (call_lo)",
            "right: dK digitals struck at each K_i super-replicate the call; the same widths "
            "struck one strike up sub-replicate it. The gap between them is the strike grid. "
            "Each slab is one digital's dK, and they stack into the staircase you collect",
        ]
        if tail_ext > 0:
            foot.append(
                f"past {ktop:,.0f} the ladder stops: both stacks go flat, the payoff does not. "
                f"That wedge is the tail model -- {atm.tail_weight:.1%} of this call's value"
            )
        if self.discount < 1.0:
            foot.append(f"areas are undiscounted; the quoted call carries the "
                        f"{self.discount:.4f} discount factor")
        foot.append(
            "greeks: dC/dK is exact -- it is minus the digital quote at K, which this "
            "ladder prices directly, and its slope-of-the-curve reading is the left panel. "
            "delta, gamma, vega and theta differentiate in F, sigma and t, which one expiry "
            "cannot pin down, so they are Black-76 at this strike's own IV"
        )
        foot += [f"warning:  {x}" for x in self.warnings]
        wrapped = _wrap(foot)

        tbl_y = py1 + ax_b + 22
        tbl, tbl_h = _value_table(self, "call", pad, w, tbl_y)
        leg_y = tbl_y + tbl_h + 30
        h = leg_y + 22 + 17 * len(wrapped) + pad

        o: list[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h:.0f}" '
            f'width="{w}" height="{h:.0f}" role="img" '
            f'aria-label="Riemann construction and ATM replicating payoff'
            f'{" for " + e(event) if event else ""}">',
            "<style>",
            "svg{color-scheme:light dark}",
            ".p{fill:#f9f9f7}.c{fill:#fcfcfb}.ink{fill:#0b0b0b}.ink2{fill:#52514e}",
            ".mut{fill:#898781}.acc{fill:#2a78d6}.acc2{fill:#eb6834}",
            ".sk{stroke:#2a78d6}.sk2{stroke:#eb6834}.fill{fill:#2a78d6}",
            ".rl{stroke:#e1e0d9}.ax{stroke:#c3c2b7}.dead{fill:#898781}",
            ".rg{stroke:rgba(11,11,11,.10)}",
            "@media(prefers-color-scheme:dark){",
            ".p{fill:#0d0d0d}.c{fill:#1a1a19}.ink{fill:#fff}.ink2{fill:#c3c2b7}",
            ".mut{fill:#898781}.acc{fill:#3987e5}.acc2{fill:#d95926}",
            ".sk{stroke:#3987e5}.sk2{stroke:#d95926}.fill{fill:#3987e5}",
            ".rl{stroke:#2c2c2a}.ax{stroke:#383835}.dead{fill:#898781}",
            ".rg{stroke:rgba(255,255,255,.10)}}",
            "</style>",
            f'<rect class="p" width="{w}" height="{h:.0f}" fill="#f9f9f7"/>',
            f'<rect class="c rg" x="{pad/2:.0f}" y="{pad/2:.0f}" width="{w-pad:.0f}" '
            f'height="{h-pad:.0f}" rx="10" fill="#fcfcfb" stroke="rgba(11,11,11,.10)"/>',
        ]

        # ---- header block ---------------------------------------------------
        o.append(_text(pad, 52, event or "ATM payoff structure", "ink",
                       size=17, mono=False, weight=600))
        sub = (f"the ATM call as an integral of {len(rows)} digitals, and as the stack "
               f"of digitals that replicates it")
        if self.expiry:
            sub += f"   ·   expiry {self.expiry:%Y-%m-%d %H:%M} UTC"
        o.append(_text(pad, 71, sub, "mut", size=11.5, mono=False))

        stats = [("forward", f"{self.forward:,.2f}"), ("ATM strike", f"{katm:,.0f}"),
                 ("call", f"{atm.call:,.2f}"),
                 ("band", f"±{atm.call_spread_width / 2:,.2f}")]
        if atm.iv:
            stats.append(("ATM IV", f"{atm.iv * 100:.1f}%"))
        sx = pad
        for label, val in stats:
            o.append(_text(sx, 99, label, "mut", size=10.5, mono=False))
            o.append(_text(sx, 121, val, "ink", size=19, weight=600))
            sx += max(len(val) * 12 + 34, 122)

        # ---- greeks strip ------------------------------------------------------
        # Second row, ruled off from the prices above it: these are sensitivities,
        # and one of them is quoted while four are fitted, which the colour says.
        o.append(f'<line class="rl" x1="{pad}" y1="136" x2="{w - pad}" y2="136" '
                 f'stroke="#e1e0d9" stroke-width="1"/>')
        gx = pad
        for label, val, exact in _greek_cells(self.greeks(atm, "call")):
            o.append(_text(gx, 157, label, "mut", size=10.5, mono=False))
            o.append(_text(gx, 179, val, "acc" if exact else "ink", size=17, weight=600))
            gx += max(len(val) * 11 + 34, 128)

        # ---- panel scaffolding ------------------------------------------------
        for px, title in ((pxa, "D(K) = Q(S > K)   ·   area right of the strike = the call"),
                          (pxb, "payoff at expiry vs settlement price")):
            o.append(_text(px - ax_l, py0 - 10, title, "ink2", size=11, mono=False, weight=600))
            if tail_ext > 0:  # the ladder ends here; everything right of it is model
                o.append(f'<rect class="dead" x="{X(px, ktop):.1f}" y="{py0}" '
                         f'width="{X(px, kend) - X(px, ktop):.1f}" height="{ph}" '
                         f'fill="#898781" opacity=".06"/>')
                o.append(f'<line class="ax" x1="{X(px, ktop):.1f}" y1="{py0}" '
                         f'x2="{X(px, ktop):.1f}" y2="{py1}" stroke="#c3c2b7" '
                         f'stroke-width="1" stroke-dasharray="2 3"/>')
            o.append(f'<line class="ax" x1="{px}" y1="{py0}" x2="{px}" y2="{py1}" '
                     f'stroke="#c3c2b7" stroke-width="1"/>')
            o.append(f'<line class="ax" x1="{px}" y1="{py1}" x2="{px+piw:.0f}" y2="{py1}" '
                     f'stroke="#c3c2b7" stroke-width="1"/>')
            # strike axis: the ladder itself, then the labelled ends
            for k in ks:
                o.append(f'<line class="ax" x1="{X(px, k):.1f}" y1="{py1}" '
                         f'x2="{X(px, k):.1f}" y2="{py1+4}" stroke="#c3c2b7" stroke-width="1"/>')
            o.append(_text(px, py1 + 18, f"{kmin:,.0f}", "mut", size=10, anchor="middle"))
            o.append(_text(X(px, ktop), py1 + 18, f"{ktop:,.0f}", "mut", size=10,
                           anchor="middle" if tail_ext > 0 else "end"))
            o.append(f'<line class="sk2" x1="{X(px, katm):.1f}" y1="{py0}" '
                     f'x2="{X(px, katm):.1f}" y2="{py1}" stroke="#eb6834" stroke-width="1" '
                     f'stroke-dasharray="3 3" opacity=".7"/>')
            if X(px, katm) - X(px, kmin) > 62:
                o.append(_text(X(px, katm), py1 + 18, f"K {katm:,.0f}", "acc2", size=10,
                               anchor="middle", weight=600))

        # ---- panel A: gridlines, curve, the two sums -------------------------
        for d in (0.0, 0.25, 0.5, 0.75, 1.0):
            o.append(f'<line class="rl" x1="{pxa}" y1="{YA(d):.1f}" x2="{pxa+piw:.0f}" '
                     f'y2="{YA(d):.1f}" stroke="#e1e0d9" stroke-width="1"/>')
            o.append(_text(pxa - 10, YA(d) + 4, f"{d:.2f}", "mut", size=10, anchor="end"))

        tail_pts = []
        if tail_ext > 0:
            tail_pts = [(X(pxa, ktop + tail_ext * i / 24.0),
                         YA(s_last * math.exp(-tail_ext * i / 24.0 / lam)))
                        for i in range(25)]

        # area = the call: down the curve from the strike, then back along the axis
        area = [(X(pxa, katm), YA(surv[ai]))]
        area += [(X(pxa, k), YA(s)) for k, s in zip(ks[ai + 1:], surv[ai + 1:])]
        area += tail_pts + [(X(pxa, kend), py1), (X(pxa, katm), py1)]
        o.append(f'<polygon class="fill" points="{_pts(area)}" fill="#2a78d6" opacity=".14"/>')

        # The left/right sums are the picture, but 60+ rectangles is a smear.
        stacked = len(ks) - 1 - ai <= 60
        if stacked:
            # Rule the shaded region at every strike.  The integral is a *sum*,
            # and one smooth wash hides that: with the risers drawn you can see
            # the dK columns the call is built out of, and read off which strikes
            # actually carry the value.
            for i in range(ai + 1, len(ks)):
                o.append(f'<line class="sk" x1="{X(pxa, ks[i]):.1f}" '
                         f'y1="{YA(surv[i]):.1f}" x2="{X(pxa, ks[i]):.1f}" '
                         f'y2="{py1}" stroke="#2a78d6" stroke-width="1" opacity=".3"/>')

            hi_step: list[tuple[float, float]] = []
            lo_step: list[tuple[float, float]] = []
            for i in range(ai, len(ks) - 1):
                x0, x1 = X(pxa, ks[i]), X(pxa, ks[i + 1])
                hi_step += [(x0, YA(surv[i])), (x1, YA(surv[i]))]
                lo_step += [(x0, YA(surv[i + 1])), (x1, YA(surv[i + 1]))]
            for step in (hi_step, lo_step):
                o.append(f'<polyline class="sk" points="{_pts(step)}" fill="none" '
                         f'stroke="#2a78d6" stroke-width="1" opacity=".55"/>')

        curve = [(X(pxa, k), YA(s)) for k, s in zip(ks, surv)]
        o.append(f'<polyline class="sk" points="{_pts(curve)}" fill="none" '
                 f'stroke="#2a78d6" stroke-width="2" stroke-linejoin="round"/>')
        if tail_pts:
            o.append(f'<polyline class="sk" points="{_pts(tail_pts)}" fill="none" '
                     f'stroke="#2a78d6" stroke-width="2" stroke-dasharray="4 3" opacity=".8"/>')

        # ---- panel B: the two stacks, and the payoff they bracket -------------
        # super: dK_i digitals at K_i.  sub: the same widths at K_{i+1}.  For
        # S in (K_m, K_m+1) they pay K_m+1 - K and K_m - K respectively.
        # Both stacks are worthless below the strike, so they run along the axis
        # and rise vertically at K -- without that second point the path would cut
        # a diagonal across the whole left half of the panel.
        cap = ktop - katm
        sup_step: list[tuple[float, float]] = [(X(pxb, kmin), YB(0.0)),
                                               (X(pxb, katm), YB(0.0))]
        sub_step: list[tuple[float, float]] = list(sup_step)
        for m in range(ai, len(ks) - 1):
            x0, x1 = X(pxb, ks[m]), X(pxb, ks[m + 1])
            sup_step += [(x0, YB(ks[m + 1] - katm)), (x1, YB(ks[m + 1] - katm))]
            sub_step += [(x0, YB(ks[m] - katm)), (x1, YB(ks[m] - katm))]
        # sup tops out on its last step; sub only pays its last digital once the
        # settlement clears the top strike, so it has one more riser to climb.
        sub_step.append((X(pxb, ktop), YB(cap)))
        sup_step.append((X(pxb, kend), YB(cap)))
        sub_step.append((X(pxb, kend), YB(cap)))

        for v in (0.0, 0.5 * cap, cap):
            o.append(f'<line class="rl" x1="{pxb}" y1="{YB(v):.1f}" x2="{pxb+piw:.0f}" '
                     f'y2="{YB(v):.1f}" stroke="#e1e0d9" stroke-width="1"/>')
            o.append(_text(pxb - 10, YB(v) + 4, f"{v:,.0f}", "mut", size=10, anchor="end"))

        # The stack is a pile, so draw it as one.  Digital m pays its own dK_m
        # forever once the settlement clears K_m+1, which is a slab of that height
        # running to the right edge; the slabs sitting on each other *are* the
        # staircase, and their total height at any S is what you collect.  Without
        # them the panel shows two outlines and the sliver between, and the gains
        # never visibly accumulate.
        if stacked:
            xend = X(pxb, kend)
            for m in range(ai, len(ks) - 1):
                y_lo, y_hi = YB(ks[m] - katm), YB(ks[m + 1] - katm)
                x0 = X(pxb, ks[m + 1])
                if xend - x0 < 0.5:      # top digital, with no tail to pay it out over
                    continue
                o.append(f'<rect class="fill" x="{x0:.1f}" y="{y_hi:.1f}" '
                         f'width="{xend - x0:.1f}" height="{y_lo - y_hi:.1f}" '
                         f'fill="#2a78d6" opacity=".1"/>')
                o.append(f'<line class="sk" x1="{x0:.1f}" y1="{y_hi:.1f}" '
                         f'x2="{xend:.1f}" y2="{y_hi:.1f}" stroke="#2a78d6" '
                         f'stroke-width="1" opacity=".22"/>')

        band = sup_step + list(reversed(sub_step))
        o.append(f'<polygon class="fill" points="{_pts(band)}" fill="#2a78d6" opacity=".14"/>')
        for step in (sup_step, sub_step):
            o.append(f'<polyline class="sk" points="{_pts(step)}" fill="none" '
                     f'stroke="#2a78d6" stroke-width="1.5" stroke-linejoin="round"/>')
        hockey = [(X(pxb, kmin), YB(0.0)), (X(pxb, katm), YB(0.0)),
                  (X(pxb, kend), YB(kend - katm))]
        o.append(f'<polyline class="sk2" points="{_pts(hockey)}" fill="none" '
                 f'stroke="#eb6834" stroke-width="2" stroke-linejoin="round"/>')
        if tail_ext > 0:
            o.append(_text(X(pxb, ktop) + 6, py0 + 14, "unquoted", "mut", size=9.5, mono=False))

        # ---- per-strike values and greeks ---------------------------------------
        o += tbl

        # ---- legend -------------------------------------------------------------
        lx = pad
        for cls, dash, label in (
            ("sk", "", "digital stacks  ·  call_hi / call_lo"),
            ("sk2", "", "true (S_T - K)+"),
            ("sk", "4 3", "modelled tail"),
        ):
            colour = "#2a78d6" if cls == "sk" else "#eb6834"
            o.append(f'<line class="{cls}" x1="{lx}" y1="{leg_y-4}" x2="{lx+22}" '
                     f'y2="{leg_y-4}" stroke="{colour}" stroke-width="2"'
                     + (f' stroke-dasharray="{dash}"' if dash else "") + "/>")
            o.append(_text(lx + 30, leg_y, label, "ink2", size=10.5, mono=False))
            lx += 30 + int(len(label) * 5.4) + 30

        # ---- footnotes -----------------------------------------------------------
        fy = leg_y + 26
        for line in wrapped:
            o.append(_text(pad, fy, line, "mut", size=10.5, mono=False))
            fy += 17

        o.append("</svg>")
        return "\n".join(o)

    def format_put_payoff_svg(self, event: str = "") -> str:
        """Draw the ATM put twice: once as an area, once as a payoff.

        The mirror of `format_payoff_svg`.  A put is the same Riemann sum walked
        in from the other end of the ladder:

            P(K) = E[(K - S_T)+] = int_0^K Q(S_T <= u) du

        so the left panel plots the CDF rather than the survival curve and shades
        the region *left* of the ATM strike -- that area is P(K_atm).  F rises,
        so the two staircases swap roles: the right-hand sum over-counts and is
        `put_hi`, the left-hand sum under-counts and is `put_lo`.

        Right panel reads those sums at expiry.  Holding dK_i digital puts struck
        at K_i+1, for every K_i below the strike, super-replicates (K - S_T)+;
        the same widths struck one strike down sub-replicate it.

        Below the bottom quoted strike both stacks go flat at K_atm - K_0 while
        the payoff keeps climbing toward K_atm.  That wedge is the lower tail --
        and it is worth exactly the chain's put at the bottom strike, since
        P(K_0) is by definition the whole area below the ladder.
        """
        e = _xml_escape
        pad, gap = 26, 34
        w = 880
        top = 190                                # header block: two stat rows
        ax_l, ax_b, ph, th = 54, 32, 250, 26     # y gutter, x gutter, plot h, panel title
        pw = (w - 2 * pad - gap) / 2
        piw = pw - ax_l
        py0 = top + th
        py1 = py0 + ph
        pxa = pad + ax_l                         # panel A plot origin
        pxb = pad + pw + gap + ax_l              # panel B plot origin

        rows = self.rows
        atm = self.atm()
        ai = rows.index(atm) if atm is not None else -1
        # The put stack is held across the strikes *below* the money, so the ATM
        # strike has to sit at least two rungs up the ladder for there to be one.
        if not self.replicable()[1]:
            return _stack_stub(
                "no put figure: the put stack is held across the strikes below K, "
                "and this ladder does not have two of them",
                self._ladder_shape(ai), event)

        ks = [r.strike for r in rows]
        cdf = [r.cdf for r in rows]
        katm = atm.strike
        kmin, ktop = ks[0], ks[-1]

        # P(K_0) is int_0^K_0 F(u) du by definition, so the bottom row's put *is*
        # the unquoted area, and divided by the CDF level there it gives the decay
        # length the lower tail model fitted.  Draw ~3 of those, capped so the
        # quoted ladder keeps most of the panel and the axis never crosses zero.
        below_area = rows[0].put / self.discount if self.discount else 0.0
        f_first = cdf[0]
        lam = below_area / f_first if f_first > 1e-9 and below_area > 0 else 0.0
        ext = min(3.0 * lam, 0.45 * (ktop - kmin), max(kmin, 0.0)) if lam > 0 else 0.0
        kstart = kmin - ext
        span = ktop - kstart or 1.0

        ymax = (katm - kstart) * 1.06 or 1.0

        def X(px: float, k: float) -> float:
            return px + (k - kstart) / span * piw

        def YA(d: float) -> float:                    # panel A: probability
            return py1 - max(0.0, min(1.0, d)) * ph

        def YB(v: float) -> float:                    # panel B: dollars of payoff
            return py1 - max(0.0, min(ymax, v)) / ymax * ph

        # The same two stacks the picture draws, priced.  put_lo drops the tail,
        # so -- like call_lo -- it is a bound that assumes nothing.
        put_lo = sum((ks[i + 1] - ks[i]) * cdf[i] for i in range(ai))
        put_hi = sum((ks[i + 1] - ks[i]) * cdf[i + 1] for i in range(ai)) + below_area
        tail_w = (self.discount * below_area / atm.put) if atm.put > 1e-12 else 0.0

        foot = [
            "left: the shaded area is P(K_atm) = int_0^K F(u) du, ruled at every strike into "
            "the dK columns it is summed from. The staircase above the curve is the right-hand "
            "sum (put_hi), the one below it the left-hand sum (put_lo)",
            "right: dK digital puts struck at each K_i+1 super-replicate the put; the same "
            "widths struck one strike down sub-replicate it. The gap between them is the grid. "
            "Each slab is one digital's dK, and they stack into the staircase you collect",
        ]
        if ext > 0:
            foot.append(
                f"below {kmin:,.0f} the ladder stops: both stacks go flat at "
                f"{katm - kmin:,.0f}, the payoff does not. That wedge is the tail model -- "
                f"{tail_w:.1%} of this put's value"
            )
        foot.append(
            "greeks: dP/dK is exact -- it is the complementary digital Q(S <= K) at K, the "
            "left panel's own curve, priced by this ladder. delta, gamma, vega and theta "
            "differentiate in F, sigma and t, which one expiry cannot pin down, so they are "
            "Black-76 at this strike's own IV"
        )
        if self.discount < 1.0:
            foot.append(f"areas are undiscounted; the quoted put carries the "
                        f"{self.discount:.4f} discount factor")
        foot += [f"warning:  {x}" for x in self.warnings]
        wrapped = _wrap(foot)

        tbl_y = py1 + ax_b + 22
        tbl, tbl_h = _value_table(self, "put", pad, w, tbl_y)
        leg_y = tbl_y + tbl_h + 30
        h = leg_y + 22 + 17 * len(wrapped) + pad

        o: list[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h:.0f}" '
            f'width="{w}" height="{h:.0f}" role="img" '
            f'aria-label="Riemann construction and ATM replicating put payoff'
            f'{" for " + e(event) if event else ""}">',
            "<style>",
            "svg{color-scheme:light dark}",
            ".p{fill:#f9f9f7}.c{fill:#fcfcfb}.ink{fill:#0b0b0b}.ink2{fill:#52514e}",
            ".mut{fill:#898781}.acc{fill:#2a78d6}.acc2{fill:#eb6834}",
            ".sk{stroke:#2a78d6}.sk2{stroke:#eb6834}.fill{fill:#2a78d6}",
            ".rl{stroke:#e1e0d9}.ax{stroke:#c3c2b7}.dead{fill:#898781}",
            ".rg{stroke:rgba(11,11,11,.10)}",
            "@media(prefers-color-scheme:dark){",
            ".p{fill:#0d0d0d}.c{fill:#1a1a19}.ink{fill:#fff}.ink2{fill:#c3c2b7}",
            ".mut{fill:#898781}.acc{fill:#3987e5}.acc2{fill:#d95926}",
            ".sk{stroke:#3987e5}.sk2{stroke:#d95926}.fill{fill:#3987e5}",
            ".rl{stroke:#2c2c2a}.ax{stroke:#383835}.dead{fill:#898781}",
            ".rg{stroke:rgba(255,255,255,.10)}}",
            "</style>",
            f'<rect class="p" width="{w}" height="{h:.0f}" fill="#f9f9f7"/>',
            f'<rect class="c rg" x="{pad/2:.0f}" y="{pad/2:.0f}" width="{w-pad:.0f}" '
            f'height="{h-pad:.0f}" rx="10" fill="#fcfcfb" stroke="rgba(11,11,11,.10)"/>',
        ]

        # ---- header block ---------------------------------------------------
        o.append(_text(pad, 52, event or "ATM put payoff structure", "ink",
                       size=17, mono=False, weight=600))
        sub = (f"the ATM put as an integral of {len(rows)} digitals, and as the stack "
               f"of digital puts that replicates it")
        if self.expiry:
            sub += f"   ·   expiry {self.expiry:%Y-%m-%d %H:%M} UTC"
        o.append(_text(pad, 71, sub, "mut", size=11.5, mono=False))

        stats = [("forward", f"{self.forward:,.2f}"), ("ATM strike", f"{katm:,.0f}"),
                 ("put", f"{atm.put:,.2f}"),
                 ("band", f"±{self.discount * (put_hi - put_lo) / 2:,.2f}")]
        if atm.iv:
            stats.append(("ATM IV", f"{atm.iv * 100:.1f}%"))
        sx = pad
        for label, val in stats:
            o.append(_text(sx, 99, label, "mut", size=10.5, mono=False))
            o.append(_text(sx, 121, val, "ink", size=19, weight=600))
            sx += max(len(val) * 12 + 34, 122)

        # ---- greeks strip ------------------------------------------------------
        # As the call figure, mirrored: the put's exact strike derivative is the
        # *complementary* digital, +DF Q(S <= K), which is the left panel's curve.
        o.append(f'<line class="rl" x1="{pad}" y1="136" x2="{w - pad}" y2="136" '
                 f'stroke="#e1e0d9" stroke-width="1"/>')
        gx = pad
        for label, val, exact in _greek_cells(self.greeks(atm, "put")):
            o.append(_text(gx, 157, label, "mut", size=10.5, mono=False))
            o.append(_text(gx, 179, val, "acc" if exact else "ink", size=17, weight=600))
            gx += max(len(val) * 11 + 34, 128)

        # ---- panel scaffolding ------------------------------------------------
        for px, title in ((pxa, "F(K) = Q(S <= K)   ·   area left of the strike = the put"),
                          (pxb, "payoff at expiry vs settlement price")):
            o.append(_text(px - ax_l, py0 - 10, title, "ink2", size=11, mono=False, weight=600))
            if ext > 0:  # the ladder starts here; everything left of it is model
                o.append(f'<rect class="dead" x="{X(px, kstart):.1f}" y="{py0}" '
                         f'width="{X(px, kmin) - X(px, kstart):.1f}" height="{ph}" '
                         f'fill="#898781" opacity=".06"/>')
                o.append(f'<line class="ax" x1="{X(px, kmin):.1f}" y1="{py0}" '
                         f'x2="{X(px, kmin):.1f}" y2="{py1}" stroke="#c3c2b7" '
                         f'stroke-width="1" stroke-dasharray="2 3"/>')
            o.append(f'<line class="ax" x1="{px}" y1="{py0}" x2="{px}" y2="{py1}" '
                     f'stroke="#c3c2b7" stroke-width="1"/>')
            o.append(f'<line class="ax" x1="{px}" y1="{py1}" x2="{px+piw:.0f}" y2="{py1}" '
                     f'stroke="#c3c2b7" stroke-width="1"/>')
            # strike axis: the ladder itself, then the labelled ends
            for k in ks:
                o.append(f'<line class="ax" x1="{X(px, k):.1f}" y1="{py1}" '
                         f'x2="{X(px, k):.1f}" y2="{py1+4}" stroke="#c3c2b7" stroke-width="1"/>')
            o.append(_text(X(px, kmin), py1 + 18, f"{kmin:,.0f}", "mut", size=10,
                           anchor="middle" if ext > 0 else "start"))
            o.append(_text(px + piw, py1 + 18, f"{ktop:,.0f}", "mut", size=10, anchor="end"))
            o.append(f'<line class="sk2" x1="{X(px, katm):.1f}" y1="{py0}" '
                     f'x2="{X(px, katm):.1f}" y2="{py1}" stroke="#eb6834" stroke-width="1" '
                     f'stroke-dasharray="3 3" opacity=".7"/>')
            if X(px, katm) - X(px, kmin) > 62 and X(px, ktop) - X(px, katm) > 62:
                o.append(_text(X(px, katm), py1 + 18, f"K {katm:,.0f}", "acc2", size=10,
                               anchor="middle", weight=600))

        # ---- panel A: gridlines, CDF, the two sums ---------------------------
        for d in (0.0, 0.25, 0.5, 0.75, 1.0):
            o.append(f'<line class="rl" x1="{pxa}" y1="{YA(d):.1f}" x2="{pxa+piw:.0f}" '
                     f'y2="{YA(d):.1f}" stroke="#e1e0d9" stroke-width="1"/>')
            o.append(_text(pxa - 10, YA(d) + 4, f"{d:.2f}", "mut", size=10, anchor="end"))

        # ordered left to right, so it lands on the ladder's bottom strike
        tail_pts = []
        if ext > 0:
            tail_pts = [(X(pxa, kstart + ext * i / 24.0),
                         YA(f_first * math.exp(-(ext - ext * i / 24.0) / lam)))
                        for i in range(25)]

        # area = the put: up from the axis, along the curve, back down at the strike
        area = [(X(pxa, kstart if tail_pts else kmin), py1)]
        area += tail_pts or [(X(pxa, kmin), YA(cdf[0]))]
        area += [(X(pxa, k), YA(f)) for k, f in zip(ks[1:ai + 1], cdf[1:ai + 1])]
        area.append((X(pxa, katm), py1))
        o.append(f'<polygon class="fill" points="{_pts(area)}" fill="#2a78d6" opacity=".14"/>')

        # The left/right sums are the picture, but 60+ rectangles is a smear.
        stacked = ai <= 60
        if stacked:
            # Rule the shaded region at every strike, as in the call figure: the
            # put is a sum of dK columns too, and the risers show which ones it
            # is actually made of.
            for i in range(ai):
                o.append(f'<line class="sk" x1="{X(pxa, ks[i]):.1f}" '
                         f'y1="{YA(cdf[i]):.1f}" x2="{X(pxa, ks[i]):.1f}" '
                         f'y2="{py1}" stroke="#2a78d6" stroke-width="1" opacity=".3"/>')

            hi_step: list[tuple[float, float]] = []
            lo_step: list[tuple[float, float]] = []
            for i in range(ai):
                x0, x1 = X(pxa, ks[i]), X(pxa, ks[i + 1])
                hi_step += [(x0, YA(cdf[i + 1])), (x1, YA(cdf[i + 1]))]
                lo_step += [(x0, YA(cdf[i])), (x1, YA(cdf[i]))]
            for step in (hi_step, lo_step):
                o.append(f'<polyline class="sk" points="{_pts(step)}" fill="none" '
                         f'stroke="#2a78d6" stroke-width="1" opacity=".55"/>')

        curve = [(X(pxa, k), YA(f)) for k, f in zip(ks, cdf)]
        o.append(f'<polyline class="sk" points="{_pts(curve)}" fill="none" '
                 f'stroke="#2a78d6" stroke-width="2" stroke-linejoin="round"/>')
        if tail_pts:
            o.append(f'<polyline class="sk" points="{_pts(tail_pts)}" fill="none" '
                     f'stroke="#2a78d6" stroke-width="2" stroke-dasharray="4 3" opacity=".8"/>')

        # ---- panel B: the two stacks, and the payoff they bracket -------------
        # super: dK_i digital puts at K_i+1.  sub: the same widths at K_i.  For
        # S in (K_m, K_m+1) they pay K_atm - K_m and K_atm - K_m+1 respectively.
        # Both are worthless at and above the strike, so they run along the axis
        # to the right of it; below the ladder both flatten at the cap.
        cap = katm - kmin
        sup_step: list[tuple[float, float]] = [(X(pxb, kstart), YB(cap))]
        sub_step: list[tuple[float, float]] = [(X(pxb, kstart), YB(cap)),
                                               (X(pxb, kmin), YB(cap))]
        for m in range(ai):
            x0, x1 = X(pxb, ks[m]), X(pxb, ks[m + 1])
            sup_step += [(x0, YB(katm - ks[m])), (x1, YB(katm - ks[m]))]
            sub_step += [(x0, YB(katm - ks[m + 1])), (x1, YB(katm - ks[m + 1]))]
        # sub has already stepped down to zero at the strike; sup pays its last
        # digital until the settlement reaches K, so it drops there instead.
        sup_step.append((X(pxb, katm), YB(0.0)))
        for step in (sup_step, sub_step):
            step.append((X(pxb, ktop), YB(0.0)))

        for v in (0.0, 0.5 * cap, cap):
            o.append(f'<line class="rl" x1="{pxb}" y1="{YB(v):.1f}" x2="{pxb+piw:.0f}" '
                     f'y2="{YB(v):.1f}" stroke="#e1e0d9" stroke-width="1"/>')
            o.append(_text(pxb - 10, YB(v) + 4, f"{v:,.0f}", "mut", size=10, anchor="end"))

        # The same pile as the call figure, mirrored: digital put m pays its own
        # dK_m for good once the settlement drops through K_m, so it is a slab of
        # that height running to the left edge.  Piled up they are the staircase,
        # and the height under you at any S is what the stack collects.
        if stacked:
            xstart = X(pxb, kstart)
            prev = cap
            for m in range(ai):
                h = katm - ks[m + 1]
                y_top, y_bot = YB(prev), YB(h)
                if X(pxb, ks[m]) - xstart < 0.5:   # bottom digital, no tail to pay over
                    prev = h
                    continue
                o.append(f'<rect class="fill" x="{xstart:.1f}" y="{y_top:.1f}" '
                         f'width="{X(pxb, ks[m]) - xstart:.1f}" '
                         f'height="{y_bot - y_top:.1f}" fill="#2a78d6" opacity=".1"/>')
                o.append(f'<line class="sk" x1="{xstart:.1f}" y1="{y_top:.1f}" '
                         f'x2="{X(pxb, ks[m]):.1f}" y2="{y_top:.1f}" stroke="#2a78d6" '
                         f'stroke-width="1" opacity=".22"/>')
                prev = h

        band = sup_step + list(reversed(sub_step))
        o.append(f'<polygon class="fill" points="{_pts(band)}" fill="#2a78d6" opacity=".14"/>')
        for step in (sup_step, sub_step):
            o.append(f'<polyline class="sk" points="{_pts(step)}" fill="none" '
                     f'stroke="#2a78d6" stroke-width="1.5" stroke-linejoin="round"/>')
        hockey = [(X(pxb, kstart), YB(katm - kstart)), (X(pxb, katm), YB(0.0)),
                  (X(pxb, ktop), YB(0.0))]
        o.append(f'<polyline class="sk2" points="{_pts(hockey)}" fill="none" '
                 f'stroke="#eb6834" stroke-width="2" stroke-linejoin="round"/>')
        if ext > 0:
            o.append(_text(X(pxb, kmin) - 6, py0 + 14, "unquoted", "mut", size=9.5,
                           mono=False, anchor="end"))

        # ---- per-strike values and greeks ---------------------------------------
        o += tbl

        # ---- legend -------------------------------------------------------------
        lx = pad
        for cls, dash, label in (
            ("sk", "", "digital put stacks  ·  put_hi / put_lo"),
            ("sk2", "", "true (K - S_T)+"),
            ("sk", "4 3", "modelled tail"),
        ):
            colour = "#2a78d6" if cls == "sk" else "#eb6834"
            o.append(f'<line class="{cls}" x1="{lx}" y1="{leg_y-4}" x2="{lx+22}" '
                     f'y2="{leg_y-4}" stroke="{colour}" stroke-width="2"'
                     + (f' stroke-dasharray="{dash}"' if dash else "") + "/>")
            o.append(_text(lx + 30, leg_y, label, "ink2", size=10.5, mono=False))
            lx += 30 + int(len(label) * 5.4) + 30

        # ---- footnotes -----------------------------------------------------------
        fy = leg_y + 26
        for line in wrapped:
            o.append(_text(pad, fy, line, "mut", size=10.5, mono=False))
            fy += 17

        o.append("</svg>")
        return "\n".join(o)


# --------------------------------------------------------------------------
# ingest: Kalshi market dicts -> Digital
# --------------------------------------------------------------------------

_ABOVE = {"greater", "greater_or_equal", "above"}
_BELOW = {"less", "less_or_equal", "below"}

# Kalshi quotes in whole cents, so 0.01 is the finest a book can express.
_TICK = 0.01

# A book wider than this carries no usable level -- a 0.00/1.00 quote has a 0.50
# midpoint that is pure fiction. Dropped unless a real trade gives us a price.
_MAX_SPREAD = 0.90

# Most strikes `_upper_tail` will regress over. Wide enough to see through tick
# quantisation, short enough that the fit stays local to the wing.
_TAIL_FIT_MAX = 16


def _prob(market: dict, *keys: str) -> float | None:
    """Read a price as a probability in [0,1].

    Kalshi currently serves decimal-dollar strings ("0.9900"); older payloads
    used integer cents (99). Accept both, keyed off the field name.
    """
    for k in keys:
        v = market.get(k)
        if v is None or v == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        return f if k.endswith("_dollars") else f / 100.0
    return None


def _quote(market: dict) -> tuple[float, float] | None:
    """(bid, ask) for the YES side as probabilities, or None if unquotable."""
    bid = _prob(market, "yes_bid_dollars", "yes_bid")
    ask = _prob(market, "yes_ask_dollars", "yes_ask")

    # The NO book is the same book seen from the other side: a resting NO ask is
    # a YES bid. On these ladders one side is often empty, so merge them.
    no_bid = _prob(market, "no_bid_dollars", "no_bid")
    no_ask = _prob(market, "no_ask_dollars", "no_ask")
    if no_ask is not None:
        implied_bid = 1.0 - no_ask
        bid = implied_bid if bid is None else max(bid, implied_bid)
    if no_bid is not None:
        implied_ask = 1.0 - no_bid
        ask = implied_ask if ask is None else min(ask, implied_ask)

    last = _prob(market, "last_price_dollars", "last_price",
                 "previous_price_dollars", "previous_price")

    # One-sided or crossed: lean on the last trade if there is one.
    if bid is None or ask is None or ask < bid:
        if last is None or last <= 0:
            return None
        bid = last if bid is None else bid
        ask = last if ask is None else ask
        if ask < bid:
            bid = ask = last

    # A book quoted 0.00 / 1.00 carries no information; its 0.50 midpoint is
    # pure fiction and would bend the whole survival curve. Drop it unless a
    # real trade gives us a level.
    if ask - bid > _MAX_SPREAD:
        if last is not None and 0.0 < last < 1.0:
            bid = ask = last
        else:
            return None

    return max(0.0, min(1.0, bid)), max(0.0, min(1.0, ask))


def digitals_from_markets(
    markets: Iterable[dict],
    warnings: list[str] | None = None,
    drop_pinned: bool = True,
) -> list[Digital]:
    """Normalise Kalshi markets into digital calls, one per distinct strike.

    Handles the three strike types Kalshi uses on crypto series:

      greater / greater_or_equal   ->  D(floor_strike) = yes
      less / less_or_equal         ->  D(cap_strike)   = 1 - yes
      between (floor, cap)         ->  a probability *bin*; the bins are pooled
                                       and cumulated from the top to rebuild the
                                       survival curve at each bin edge.

    Threshold markets win over bin-derived levels at the same strike, since they
    quote the survival probability directly instead of via a running sum.

    `drop_pinned` discards threshold strikes whose book rests on the extreme tick
    (0.00/0.01 or 0.99/1.00).  Their midpoints are grid artefacts and integrating
    a long dead wing of them adds a large constant to every call -- see `_pinned`.
    Bins are exempt: they are a partition, so dropping members would break both
    the contiguity check and the unit-mass constraint that `_from_bins` projects
    onto, and that projection already handles dead bins correctly.
    """
    thresholds: list[Digital] = []
    bins: list[tuple[float, float, float, float, dict]] = []  # floor, cap, bid, ask, mkt

    for m in markets:
        q = _quote(m)
        if q is None:
            continue
        bid, ask = q
        stype = str(m.get("strike_type", "")).lower()
        floor_k = m.get("floor_strike")
        cap_k = m.get("cap_strike")

        if stype in _ABOVE and floor_k is not None:
            thresholds.append(_mk(float(floor_k), bid, ask, m))
        elif stype in _BELOW and cap_k is not None:
            # flip: P(S > K) = 1 - P(S <= K); sides swap
            thresholds.append(_mk(float(cap_k), 1.0 - ask, 1.0 - bid, m))
        elif floor_k is not None and cap_k is not None:
            bins.append((float(floor_k), float(cap_k), bid, ask, m))
        elif floor_k is not None:  # untyped but one-sided: treat as "above"
            thresholds.append(_mk(float(floor_k), bid, ask, m))
        elif cap_k is not None:
            thresholds.append(_mk(float(cap_k), 1.0 - ask, 1.0 - bid, m))

    # The outer `less`/`greater` buckets that book-end a bin ladder are part of
    # the same partition, so the interior bins only account for what they leave.
    total = 1.0
    if bins:
        lo_edge, hi_edge = min(b[0] for b in bins), max(b[1] for b in bins)
        for d in thresholds:
            if d.strike <= lo_edge:      # P(S <= lo_edge) sits below the ladder
                total -= max(0.0, 1.0 - d.mid)
            elif d.strike >= hi_edge:    # P(S > hi_edge) sits above it
                total -= max(0.0, d.mid)
        total = min(max(total, 0.0), 1.0)

    # After `total`, so a pinned book-end bucket still gets to claim its sliver
    # of mass off the bin ladder before it stops being a strike of its own.
    if drop_pinned and thresholds:
        live = [d for d in thresholds if not _pinned(d)]
        dropped = len(thresholds) - len(live)
        if dropped and warnings is not None:
            # How much strike space the dead wings spanned: that times the half-tick
            # midpoint is the constant this would have added to every call.
            wing = 0.0
            if live:
                lo, hi = min(d.strike for d in live), max(d.strike for d in live)
                ks = [d.strike for d in thresholds]
                wing = max(max(ks) - hi, 0.0) + max(lo - min(ks), 0.0)
            warnings.append(
                f"dropped {dropped} of {len(thresholds)} threshold strikes pinned at the "
                f"extreme tick; integrating their midpoints would have added ~"
                f"${0.5 * _TICK * wing:,.0f} to every call. The region beyond the live "
                f"quotes is the tail model's job now"
            )
        thresholds = live

    out = {d.strike: d for d in _from_bins(bins, warnings=warnings, total=total)}
    out.update({d.strike: d for d in thresholds})  # thresholds quote D directly
    return sorted(out.values(), key=lambda d: d.strike)


def _pinned(d: Digital) -> bool:
    """True when a digital's book sits on the extreme tick and says nothing.

    A far-OTM strike quoted 0.00 / 0.01 is the market saying "worthless, and the
    tick cannot express less".  Its 0.005 midpoint is an artefact of the grid,
    not a probability -- but `build_chain` *integrates* D(K), so that artefact is
    multiplied by however much strike space the dead wing spans.  A 189-strike
    ladder running $9,500 past the money at 100-wide strikes contributes
    0.005 * 9,500 = $47.50 to every call on the board, which then reads back as a
    large, symmetric, entirely fictitious smile.  The error is one-signed, so it
    does not average out, and it is invisible to the monotonicity repair: a flat
    0.005 is perfectly non-increasing.

    Dropping these leaves the region beyond the live quotes to the tail model,
    which is what it exists for.  See `_upper_tail` / `_lower_tail`.
    """
    return (d.bid <= 0.0 and d.ask <= _TICK) or (d.bid >= 1.0 - _TICK and d.ask >= 1.0)


def _mk(strike: float, bid: float, ask: float, m: dict, mid: float | None = None) -> Digital:
    return Digital(
        strike=strike,
        bid=max(0.0, min(1.0, bid)),
        ask=max(0.0, min(1.0, ask)),
        ticker=str(m.get("ticker", "")),
        mid_override=None if mid is None else max(0.0, min(1.0, mid)),
    )


def _project_to_simplex(
    mids: Sequence[float], bids: Sequence[float], asks: Sequence[float], total: float = 1.0
) -> list[float] | None:
    """Pick p_i in [bid_i, ask_i] summing to `total`, shifted least from `mids`.

    p_i(t) = clip(mid_i + t, bid_i, ask_i) is non-decreasing in t, so one scalar
    bisection lands the constraint.  Returns None if no feasible p exists (the
    quoted boxes are mutually inconsistent).
    """
    def summed(t: float) -> float:
        return sum(min(max(m + t, b), a) for m, b, a in zip(mids, bids, asks))

    lo, hi = -1.0, 1.0
    while summed(lo) > total and lo > -1e3:
        lo *= 2.0
    while summed(hi) < total and hi < 1e3:
        hi *= 2.0

    # Saturated ends are the all-bids / all-asks corners.  A ladder whose bids
    # sum to exactly `total` is feasible at that corner, so compare with a
    # tolerance -- otherwise a 1e-16 float hair reads as infeasible and drops us
    # onto the raw-midpoint path this function exists to avoid.
    tol = 1e-9 * max(1.0, len(mids))
    if summed(lo) > total + tol or summed(hi) < total - tol:
        return None
    total = min(max(total, summed(lo)), summed(hi))
    for _ in range(200):
        t = 0.5 * (lo + hi)
        if summed(t) < total:
            lo = t
        else:
            hi = t
    t = 0.5 * (lo + hi)
    return [min(max(m + t, b), a) for m, b, a in zip(mids, bids, asks)]


def _from_bins(
    bins: Sequence[tuple[float, float, float, float, dict]],
    warnings: list[str] | None = None,
    total: float = 1.0,
) -> list[Digital]:
    """Rebuild D(K) at each bin's lower edge by cumulating bin mass downward.

    For an exhaustive, mutually exclusive ladder, Q(S > floor_i) = sum_{j>=i} p_j.

    The catch is that naive midpoint cumulation does not survive contact with a
    real book.  A crypto ladder runs ~190 bins and all but a couple sit at
    0.00 bid / 0.01 ask; taking each one's 0.005 midpoint injects ~0.9 of
    fictitious mass, which smears the survival curve across every strike and
    drags the forward far off spot.  Since the bins partition the outcome space
    their masses must sum to 1, so we project the midpoints onto that constraint
    inside each bin's own bid/ask box: worthless bins clip down to their zero
    bid and the bins that actually trade keep their mass.

    Cumulated bid/ask are retained as (loose) bounds -- summing ~190 one-cent
    asks saturates the upper one, so on bin ladders trust the mid.
    """
    if not bins:
        return []
    ordered = sorted(bins, key=lambda b: b[0])

    # Contiguity check: Kalshi caps a bin one tick below the next floor.
    gaps = [
        ordered[i + 1][0] - ordered[i][1]
        for i in range(len(ordered) - 1)
    ]
    span = ordered[-1][1] - ordered[0][0]
    contiguous = all(abs(g) <= max(1.0, 0.001 * span) for g in gaps) if gaps else True

    bids = [b[2] for b in ordered]
    asks = [b[3] for b in ordered]
    mids = [0.5 * (b + a) for b, a in zip(bids, asks)]

    masses = _project_to_simplex(mids, bids, asks, total) if contiguous else None
    if masses is None:
        masses = mids
        if warnings is not None:
            warnings.append(
                "bin ladder is not a normalisable partition; cumulating raw "
                "midpoints, which over-counts mass on wide/empty bins"
            )
    elif warnings is not None and abs(sum(mids) - total) > 0.02:
        warnings.append(
            f"bin midpoints summed to {sum(mids):.2f} instead of {total:.2f}; "
            f"renormalised onto the quoted bid/ask boxes"
        )

    out: list[Digital] = []
    cum_bid = cum_ask = cum_mid = 0.0
    for (floor_k, _cap, bid, ask, m), p in zip(reversed(ordered), reversed(masses)):
        cum_bid += bid
        cum_ask += ask
        cum_mid += p
        out.append(_mk(floor_k, cum_bid, cum_ask, m, mid=cum_mid))
    out.reverse()
    return out


# --------------------------------------------------------------------------
# arbitrage repair
# --------------------------------------------------------------------------

def _monotone_decreasing(values: Sequence[float]) -> list[float]:
    """Pool-adjacent-violators projection onto non-increasing sequences.

    D(K) must fall with K; crossed or stale quotes routinely break that, and an
    unrepaired violation shows up as a negative density downstream.
    """
    blocks: list[list[float]] = []   # [sum, count]
    for v in values:
        blocks.append([v, 1.0])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] < blocks[-1][0] / blocks[-1][1]:
            s, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
    out: list[float] = []
    for s, c in blocks:
        out.extend([s / c] * int(c))
    return out


# --------------------------------------------------------------------------
# Black-76 inversion
# --------------------------------------------------------------------------

def _black76_call(f: float, k: float, tau: float, sigma: float, df: float) -> float:
    if tau <= 0 or sigma <= 0 or f <= 0 or k <= 0:
        return df * max(f - k, 0.0)
    v = sigma * math.sqrt(tau)
    d1 = (math.log(f / k) + 0.5 * v * v) / v
    return df * (f * _norm_cdf(d1) - k * _norm_cdf(d1 - v))


def _black76_greeks(f: float, k: float, tau: float, sigma: float, df: float,
                    kind: Literal["call", "put"]) -> tuple[float, float, float, float]:
    """(delta, gamma, vega, theta) under Black-76, in *quoting* units.

    vega is per vol point and theta per day, since that is how both get read;
    the raw partials are per 1.00 of vol and per year.  The carry rate is
    recovered from the discount factor rather than passed, so theta stays
    consistent with whatever `rate=` built the chain -- at the default r = 0 its
    second term vanishes and theta is pure gamma rent.
    """
    if tau <= 0 or sigma <= 0 or f <= 0 or k <= 0:
        return 0.0, 0.0, 0.0, 0.0
    v = sigma * math.sqrt(tau)
    d1 = (math.log(f / k) + 0.5 * v * v) / v
    d2 = d1 - v
    n1 = _norm_pdf(d1)
    r = -math.log(df) / tau if 0.0 < df < 1.0 else 0.0

    # Both live options share gamma and vega: the two payoffs differ by the
    # forward itself, which is linear in F and flat in sigma.
    gamma = df * n1 / (f * v)
    vega = df * f * n1 * math.sqrt(tau)
    decay = df * f * n1 * sigma / (2.0 * math.sqrt(tau))   # the shared time value

    if kind == "call":
        delta = df * _norm_cdf(d1)
        price = df * (f * _norm_cdf(d1) - k * _norm_cdf(d2))
    else:
        delta = -df * _norm_cdf(-d1)
        price = df * (k * _norm_cdf(-d2) - f * _norm_cdf(-d1))
    theta = r * price - decay

    return delta, gamma, vega / 100.0, theta / 365.0


def _density_vol(rows: Sequence[ChainRow], forward: float, tau: float) -> float | None:
    """Vol read off the *peak* of the reconstructed density.

    A lognormal density peaks at 0.3989 / (F sigma sqrt(tau)), so inverting the
    tallest pdf gives a vol that depends only on the strikes around the money.
    That is the point: it is a purely *local* measurement, where the `call`
    column is an integral across the entire ladder.  The two must agree, and
    when they do not it is the wings, not the shape, that are wrong -- see
    `_vol_consistency`.
    """
    if tau <= 0 or forward <= 0 or not rows:
        return None
    peak = max(r.pdf for r in rows)
    if peak <= 0:
        return None
    return 0.3989 / (peak * forward * math.sqrt(tau))


# How far the call column may sit above the density before it is worth saying so.
# Real skew moves these apart a little -- the threshold ladders run ~1.1 -- while
# a dead wing being integrated runs 1.7 and up.
_VOL_GAP = 1.25


def _vol_consistency(rows: Sequence[ChainRow], forward: float, tau: float) -> str | None:
    """Warn when the call column implies far more vol than the density does.

    Two readings of one distribution: the density is a local derivative of D(K)
    around the money, the call column an integral across the whole ladder.  Mass
    sitting on strikes that are not really quoted inflates the integral while
    leaving the shape alone, so the two come apart -- and they do it without
    disturbing the forward, since the wings' errors cancel in E[S].  A plausible
    forward is therefore no evidence at all; this is.

    What it catches: a ladder whose whole call column is scaled up, which is the
    bin-ladder failure -- ~190 bins, the projection leaves slivers on the dead
    ones, and integrating those slivers across a ladder running +/-15% lifts the
    ATM call to ~1.8x the density's.  Sensitivity floor is around 8-10% of mass
    smeared; below that the ratio sits inside the range real skew produces.

    What it does NOT catch: tick-pinned quotes adding a *constant* to every call.
    A constant is small next to a fat ATM call (1.17x on the synthetic in
    `selftest` step 11) and enormous next to a wing call (1.95x at 5% OTM), so
    it barely moves this ratio.  `_pinned` drops those quotes on the way in and
    `tail_weight` flags what is left; do not read silence here as covering them.
    """
    dens = _density_vol(rows, forward, tau)
    if dens is None or dens <= 0:
        return None
    atm = min(rows, key=lambda r: abs(r.strike - forward))
    if atm.iv is None or atm.iv <= 0:
        return None
    ratio = atm.iv / dens
    if ratio <= _VOL_GAP:
        return None
    return (
        f"the call column implies {atm.iv * 100:.0f}% vol at the money but the "
        f"density implies {dens * 100:.0f}% ({ratio:.1f}x); value is coming from "
        f"strikes the shape of the distribution does not support, so treat the "
        f"call/put columns as an upper bound"
    )


def _implied_vol(price: float, f: float, k: float, tau: float, df: float) -> float | None:
    """Bisection on Black-76.  Returns None outside the no-arbitrage band."""
    if tau <= 0 or f <= 0 or k <= 0:
        return None
    intrinsic = df * max(f - k, 0.0)
    if price <= intrinsic + 1e-12 or price >= df * f - 1e-12:
        return None
    lo, hi = 1e-6, 10.0
    if _black76_call(f, k, tau, hi, df) < price:
        return None
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _black76_call(f, k, tau, mid, df) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------
# the build
# --------------------------------------------------------------------------

def build_chain(
    markets: Iterable[dict] | Iterable[Digital],
    *,
    expiry: datetime | None = None,
    now: datetime | None = None,
    rate: float = 0.0,
    side: Side = "mid",
    min_strikes: int = 3,
) -> OptionChain:
    """Riemann-integrate a ladder of Kalshi binaries into a vanilla chain.

    Args:
        markets: raw /markets dicts, or pre-built `Digital`s.
        expiry:  settlement time.  Read from the markets if omitted.
        now:     valuation time; defaults to UTC now.
        rate:    continuously-compounded discount rate.  0 is the right default
                 for the hourly/daily crypto series -- carry over a few hours is
                 far inside the tick.
        side:    which digital quote drives the integral.  'bid' and 'ask' give
                 you an executable band around the 'mid' chain.
        min_strikes: below this many usable strikes, return an empty chain
                 rather than a fake one.

    The tail is always fitted (exponential decay off the last two points) and
    D(K) is always PAVA-repaired before integrating.  Both used to be switchable
    and neither switch was ever worth throwing: truncating the tail biases the
    forward and every call low, and integrating a non-monotone D(K) produces
    negative densities downstream.

    Returns:
        OptionChain, one row per strike, plus the implied forward and any
        warnings worth surfacing before you trade on it.
    """
    warnings: list[str] = []
    market_list = list(markets)

    if market_list and isinstance(market_list[0], Digital):
        digitals: list[Digital] = sorted(market_list, key=lambda d: d.strike)  # type: ignore[arg-type]
    else:
        digitals = digitals_from_markets(market_list, warnings=warnings)  # type: ignore[arg-type]
        if expiry is None:
            expiry = _infer_expiry(market_list)  # type: ignore[arg-type]

    now = now or datetime.now(timezone.utc)
    tau = 0.0
    if expiry is not None:
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        tau = max((expiry - now).total_seconds(), 0.0) / (365.0 * 24 * 3600)
        if tau <= 0:
            warnings.append("expiry is in the past; implied vols suppressed")
    else:
        warnings.append("no expiry available; implied vols suppressed")

    df = math.exp(-rate * tau)

    if len(digitals) < min_strikes:
        warnings.append(f"only {len(digitals)} usable strikes (need {min_strikes})")
        return OptionChain(forward=0.0, expiry=expiry, tau=tau, discount=df, warnings=warnings)

    # Only bin-derived levels carry mid_override, so this is the tell that the
    # sided curves came from cumulating ~190 one-cent quotes -- which saturates
    # the ask side and floors the bid side. The mid is renormalised and fine;
    # the sided ones are not, and silently return a nonsense forward.
    if side != "mid" and any(d.mid_override is not None for d in digitals):
        warnings.append(
            f"'{side}' curve is cumulated from bin quotes and is not renormalised; "
            f"the forward and vols here are unreliable -- use side='mid'"
        )

    strikes = [d.strike for d in digitals]
    surv = [max(0.0, min(1.0, d.price(side))) for d in digitals]

    fixed = _monotone_decreasing(surv)
    if any(abs(a - b) > 1e-9 for a, b in zip(surv, fixed)):
        warnings.append("digital curve was not monotone; PAVA-repaired before integrating")
    surv = fixed

    # ---- tails -----------------------------------------------------------
    # Upper: int_{K_n}^inf S(u) du.  Lower: int_0^{K_0} S(u) du, which is K_0
    # minus whatever mass already sits below the bottom quoted strike.
    upper_tail = _upper_tail(strikes, surv)
    lower_tail = _lower_tail(strikes, surv)
    truncated = (1.0 - surv[0]) + surv[-1]
    if truncated > 0.05:
        warnings.append(
            f"{truncated:.1%} of probability mass sits outside the quoted strikes; "
            f"forward and wing calls lean on the fitted tail"
        )

    n = len(strikes)

    # ---- integrate, cumulating downward from the top strike --------------
    # C(K_j) = sum_{i>=j} int_{K_i}^{K_i+1} S(u) du + tail.  S is decreasing, so
    # the left rule over-counts and the right rule under-counts: free bounds.
    # `call_lo` deliberately omits the tail: E[(S-K)+] can only shrink when you
    # discard mass above the top strike, so right-Riemann-without-tail is a real
    # bound that assumes nothing. There is no matching rigorous upper bound --
    # unquoted mass can sit arbitrarily far out -- so `call_hi` carries the
    # modelled tail and is an estimate. `tail_weight` says where that matters.
    call_mid = [0.0] * n
    call_lo = [0.0] * n
    call_hi = [0.0] * n
    acc_mid = acc_hi = upper_tail
    acc_lo = 0.0
    call_mid[n - 1] = acc_mid
    call_lo[n - 1] = acc_lo
    call_hi[n - 1] = acc_hi
    for i in range(n - 2, -1, -1):
        dk = strikes[i + 1] - strikes[i]
        acc_mid += 0.5 * (surv[i] + surv[i + 1]) * dk
        acc_lo += surv[i + 1] * dk
        acc_hi += surv[i] * dk
        call_mid[i] = acc_mid
        call_lo[i] = acc_lo
        call_hi[i] = acc_hi

    forward = call_mid[0] + lower_tail  # E[S] = int_0^inf S(u) du

    # ---- density: pdf(K) = -dD/dK, central differences on the interior ----
    pdf = [0.0] * n
    for i in range(n):
        if i == 0:
            pdf[i] = (surv[0] - surv[1]) / (strikes[1] - strikes[0])
        elif i == n - 1:
            pdf[i] = (surv[n - 2] - surv[n - 1]) / (strikes[n - 1] - strikes[n - 2])
        else:
            pdf[i] = (surv[i - 1] - surv[i + 1]) / (strikes[i + 1] - strikes[i - 1])
        pdf[i] = max(pdf[i], 0.0)

    rows: list[ChainRow] = []
    for i, d in enumerate(digitals):
        c = df * call_mid[i]
        put = c - df * (forward - strikes[i])  # put-call parity, same curve
        rows.append(
            ChainRow(
                strike=strikes[i],
                digital_bid=d.bid,
                digital_mid=d.mid,
                digital_ask=d.ask,
                cdf=1.0 - surv[i],
                pdf=pdf[i],
                call=c,
                call_lo=df * call_lo[i],
                call_hi=df * call_hi[i],
                put=max(put, 0.0),
                iv=_implied_vol(c, forward, strikes[i], tau, df) if tau > 0 else None,
                tail_weight=(upper_tail / call_mid[i]) if call_mid[i] > 1e-12 else 0.0,
                ticker=d.ticker,
            )
        )

    gap = _vol_consistency(rows, forward, tau)
    if gap:
        warnings.append(gap)

    return OptionChain(
        forward=forward,
        expiry=expiry,
        tau=tau,
        discount=df,
        rows=rows,
        truncated_mass=truncated,
        warnings=warnings,
    )


def _upper_tail(strikes: Sequence[float], surv: Sequence[float]) -> float:
    """int_{K_n}^inf S(u) du, assuming S decays exponentially past the last strike.

    The decay rate comes from a log-linear least-squares fit over the last few
    strikes rather than the final pair: on a live book the top strike is the
    thinnest and stalest quote, and letting it alone set the rate makes the whole
    wing swing with one tick.

    The window has to span a real decay, though. Out where the book trades for a
    cent or two, tick quantisation flattens the curve into a staircase -- five
    consecutive strikes all quoted 0.01/0.02 give an exactly flat segment, so
    sxy = 0, the guard below rejects it, and the fit degenerates to a single grid
    step. That understates the wing several-fold and, because the tail is a
    constant of integration, drags down every call on the board. So walk the
    window back until the curve has actually halved.
    """
    s_last = surv[-1]
    if s_last <= 1e-9:
        return 0.0

    live = [(k, s) for k, s in zip(strikes, surv) if s > 1e-9]
    pts = live[-4:]
    for n in range(4, min(len(live), _TAIL_FIT_MAX) + 1):
        pts = live[-n:]
        if pts[0][1] >= 2.0 * s_last:  # bounded, so a genuinely flat book terminates
            break
    lam = 0.0
    if len(pts) >= 2:
        # regress ln S = a + b*K; the exponential scale is lam = -1/b
        xs = [p[0] for p in pts]
        ys = [math.log(p[1]) for p in pts]
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        if sxx > 0 and sxy < 0:
            lam = -sxx / sxy

    if lam <= 0:  # non-decaying or degenerate: fall back to one grid step
        return s_last * (strikes[-1] - strikes[-2])
    return s_last * lam


def _lower_tail(strikes: Sequence[float], surv: Sequence[float]) -> float:
    """int_0^{K_0} S(u) du -- what sits below the bottom quoted strike.

    For a ladder that brackets the market, S ~ 1 down there and the integral is
    just K_0; the correction is the little mass already below K_0.
    """
    below = 1.0 - surv[0]
    if below <= 1e-9:
        return strikes[0]
    s_next = surv[1]
    denom = math.log((1.0 - s_next) / below) if (1.0 - s_next) > below > 0 else 0.0
    if denom <= 0:
        return strikes[0] - below * (strikes[1] - strikes[0])
    lam = (strikes[1] - strikes[0]) / denom
    return max(strikes[0] - below * lam, 0.0)


def _infer_expiry(markets: Sequence[dict]) -> datetime | None:
    # close_time first: for the crypto series that is the price-observation
    # moment. `expiration_time` is a much later settlement deadline (it can sit
    # a week out) and using it would understate vol by a large factor.
    for key in ("close_time", "expected_expiration_time", "occurrence_datetime", "expiration_time"):
        for m in markets:
            raw = m.get(key)
            if raw:
                try:
                    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                except ValueError:
                    continue
    return None


# --------------------------------------------------------------------------
# self-test: price binaries off a known lognormal, then invert them
# --------------------------------------------------------------------------
# If the reconstruction is right, feeding it digitals priced off a lognormal
# with vol sigma must give back the forward, the Black-76 call values, and
# sigma itself.  Runs offline -- no API key, no network.

_F0, _SIGMA, _TAU = 100_000.0, 0.60, 1.0 / 365.0
_NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)
_EXPIRY = _NOW + timedelta(days=1)


def _lognormal_digitals(strikes: Sequence[float], spread: float = 0.0) -> list[Digital]:
    """True Q(S > K) under Black-76, optionally wrapped in a bid/ask."""
    out = []
    for k in strikes:
        v = _SIGMA * math.sqrt(_TAU)
        d2 = (math.log(_F0 / k) - 0.5 * v * v) / v
        p = _norm_cdf(d2)
        out.append(Digital(strike=k, bid=max(0.0, p - spread), ask=min(1.0, p + spread)))
    return out


def _lognormal_threshold_markets(strikes: Sequence[float]) -> list[dict]:
    """The same lognormal, but quoted as Kalshi 'greater' markets on a 1c grid.

    Rounding to the tick is the point: out in the wings every book collapses onto
    0.00/0.01, which is the failure `_pinned` exists to catch.
    """
    out = []
    for k in strikes:
        v = _SIGMA * math.sqrt(_TAU)
        d2 = (math.log(_F0 / k) - 0.5 * v * v) / v
        p = _norm_cdf(d2)
        bid = math.floor(p / _TICK) * _TICK
        out.append({"ticker": f"K{k:.0f}", "strike_type": "greater", "floor_strike": k,
                    "yes_bid_dollars": f"{bid:.4f}",
                    "yes_ask_dollars": f"{min(bid + _TICK, 1.0):.4f}"})
    return out


def _check(name: str, got: float, want: float, tol: float) -> bool:
    err = abs(got - want)
    rel = err / abs(want) if want else err
    ok = err <= tol or rel <= tol
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got:,.4f} want {want:,.4f} (err {err:,.4g})")
    return ok


def _slabs_tile(root, expected: int, label: str) -> bool:
    """The payoff panel's slabs must tile the stack: one per digital, no gaps.

    Each slab is one digital's dK drawn where that digital pays, so stacked they
    have to close up into the staircase exactly -- a gap or an overlap would be
    drawing a payoff nobody holds.  Checked in pixels, since that is what the
    reader is actually asked to believe.
    """
    slabs = sorted(
        (float(r.get("y")), float(r.get("height")))
        for r in root.findall(".//{http://www.w3.org/2000/svg}rect")
        if r.get("class") == "fill"
    )
    joins = all(abs(slabs[i][0] + slabs[i][1] - slabs[i + 1][0]) < 0.6
                for i in range(len(slabs) - 1))
    positive = all(h > 0 for _, h in slabs)
    good = len(slabs) == expected and joins and positive
    print(f"  [{'PASS' if good else 'FAIL'}] the {label} slabs tile the stack "
          f"({len(slabs)} of {expected} digitals, no gaps)")
    return good


def selftest() -> int:
    ok = True
    print("\n1. forward + IV recovery on a dense strike grid (250-wide, +/- 4 sigma)")
    strikes = [_F0 - 12_000 + 250 * i for i in range(97)]
    chain = build_chain(_lognormal_digitals(strikes), expiry=_EXPIRY, now=_NOW)
    ok &= _check("implied forward", chain.forward, _F0, 2e-3)

    # Two different questions, two different metrics.  Relative error on a
    # sub-dollar wing call is dust-division -- the trapezoid rule carries a small
    # positive bias (it over-counts a convex decreasing integrand) whose ABSOLUTE
    # size shrinks into the wing while its ratio to a vanishing call blows up.
    # So: bound the absolute error against notional everywhere, and the relative
    # error only where the option is worth something.
    worst_iv = worst_abs = worst_rel = 0.0
    for r in chain.rows:
        want_call = _black76_call(_F0, r.strike, _TAU, _SIGMA, 1.0)
        worst_abs = max(worst_abs, abs(r.call - want_call))
        if want_call > 0.001 * _F0:  # calls worth >$100 on a $100k forward
            worst_rel = max(worst_rel, abs(r.call - want_call) / want_call)
        if r.iv is not None and 0.02 < r.cdf < 0.98:
            worst_iv = max(worst_iv, abs(r.iv - _SIGMA))
    ok &= _check("worst abs. call error / forward", worst_abs / _F0, 0.0, 1e-4)
    ok &= _check("worst rel. call error (calls >0.1% of F)", worst_rel, 0.0, 5e-3)
    ok &= _check("worst abs. IV error (2%-98% CDF)", worst_iv, 0.0, 5e-3)

    print("\n2. call_lo is a rigorous lower bound; [lo,hi] brackets where quotes drive")
    bound_ok = bracket_ok = True
    n_bracket = 0
    for r in chain.rows:
        want = _black76_call(_F0, r.strike, _TAU, _SIGMA, 1.0)
        if r.call_lo > want + 1e-9:  # must hold at EVERY strike, tail or not
            bound_ok = False
            print(f"  [FAIL] K={r.strike:,.0f}: lower bound {r.call_lo:,.4f} exceeds true {want:,.4f}")
        if not r.model_dependent:
            n_bracket += 1
            if not (r.call_lo - 1e-6 <= want <= r.call_hi + 1e-6):
                bracket_ok = False
                print(f"  [FAIL] K={r.strike:,.0f}: {want:,.4f} outside [{r.call_lo:,.4f}, {r.call_hi:,.4f}]")
    print(f"  [{'PASS' if bound_ok else 'FAIL'}] call_lo <= true call at all {len(chain.rows)} strikes")
    print(f"  [{'PASS' if bracket_ok else 'FAIL'}] bracketed at {n_bracket} quote-driven strikes")
    ok &= bound_ok and bracket_ok

    tailheavy = [r for r in chain.rows if r.model_dependent]
    print(f"         ({len(tailheavy)} rows flagged model_dependent, from K="
          f"{min(r.strike for r in tailheavy):,.0f})" if tailheavy else "         (no rows tail-dominated)")

    print("\n3. coarse grid (2000-wide) degrades but stays sane")
    coarse = [_F0 - 12_000 + 2_000 * i for i in range(13)]
    cc = build_chain(_lognormal_digitals(coarse), expiry=_EXPIRY, now=_NOW)
    ok &= _check("coarse implied forward", cc.forward, _F0, 5e-3)
    ok &= _check("coarse ATM IV", cc.atm().iv, _SIGMA, 0.05)

    print("\n4. put-call parity holds on every row")
    parity_ok = True
    for r in chain.rows:
        lhs, rhs = r.call - r.put, chain.discount * (chain.forward - r.strike)
        if abs(lhs - rhs) > 1e-6 and r.put > 0:
            parity_ok = False
            print(f"  [FAIL] K={r.strike:,.0f}: C-P={lhs:,.4f} vs F-K={rhs:,.4f}")
    print(f"  [{'PASS' if parity_ok else 'FAIL'}] parity within 1e-6")
    ok &= parity_ok

    print("\n5. density integrates to ~1 and peaks near the forward")
    total = 0.0
    for i in range(len(chain.rows) - 1):
        a, b = chain.rows[i], chain.rows[i + 1]
        total += 0.5 * (a.pdf + b.pdf) * (b.strike - a.strike)
    ok &= _check("integral of pdf over quoted range", total, 1.0 - chain.truncated_mass, 1e-2)
    peak = max(chain.rows, key=lambda r: r.pdf)
    ok &= _check("pdf mode", peak.strike, _F0, 0.02)

    print("\n6. arbitrage repair: PAVA forces a non-increasing curve")
    fixed = _monotone_decreasing([0.9, 0.5, 0.6, 0.3])  # 0.5/0.6 is crossed
    mono = all(fixed[i] >= fixed[i + 1] - 1e-12 for i in range(len(fixed) - 1))
    print(f"  [{'PASS' if mono else 'FAIL'}] {[round(v, 4) for v in fixed]} is non-increasing")
    ok &= mono
    ok &= _check("PAVA preserves total mass", sum(fixed), 0.9 + 0.5 + 0.6 + 0.3, 1e-9)

    crossed = [Digital(strike=k, bid=p, ask=p) for k, p in
               [(90_000, 0.9), (95_000, 0.5), (100_000, 0.6), (105_000, 0.3), (110_000, 0.1)]]
    rep = build_chain(crossed, expiry=_EXPIRY, now=_NOW)
    neg = [r for r in rep.rows if r.pdf < 0]
    print(f"  [{'PASS' if not neg else 'FAIL'}] repaired chain has no negative density")
    print(f"         warning surfaced: {any('monotone' in w for w in rep.warnings)}")
    ok &= not neg

    print("\n7. Kalshi market-dict ingest: above / below / between")
    mkts = [
        {"ticker": "A", "strike_type": "greater", "floor_strike": 100_000,
         "yes_bid": 48, "yes_ask": 52},
        {"ticker": "B", "strike_type": "less", "cap_strike": 95_000,
         "yes_bid": 20, "yes_ask": 24},  # P(S<95k) in [.20,.24] -> D(95k) in [.76,.80]
    ]
    ds = {d.strike: d for d in digitals_from_markets(mkts)}
    ok &= _check("above 100k -> mid", ds[100_000].mid, 0.50, 1e-9)
    ok &= _check("below 95k flipped -> bid", ds[95_000].bid, 0.76, 1e-9)
    ok &= _check("below 95k flipped -> ask", ds[95_000].ask, 0.80, 1e-9)

    bins = [
        {"ticker": "b1", "strike_type": "between", "floor_strike": 90_000, "cap_strike": 95_000,
         "yes_bid": 19, "yes_ask": 21},
        {"ticker": "b2", "strike_type": "between", "floor_strike": 95_000, "cap_strike": 100_000,
         "yes_bid": 29, "yes_ask": 31},
        {"ticker": "b3", "strike_type": "between", "floor_strike": 100_000, "cap_strike": 105_000,
         "yes_bid": 49, "yes_ask": 51},
    ]
    bd = {d.strike: d for d in digitals_from_markets(bins)}
    ok &= _check("bins cumulate: D(100k)", bd[100_000].mid, 0.50, 1e-9)
    ok &= _check("bins cumulate: D(95k)", bd[95_000].mid, 0.80, 1e-9)
    # bottom edge of an exhaustive ladder: mid must be 1.0 even though the
    # cumulated ask (1.03) clamps -- that's what mid_override is for
    ok &= _check("bins cumulate: D(90k)", bd[90_000].mid, 1.00, 1e-9)
    ok &= _check("bins: clamped ask bound", bd[90_000].ask, 1.00, 1e-9)

    print("\n8. bid/ask sides bracket the mid chain")
    wide = _lognormal_digitals(strikes, spread=0.02)
    lo = build_chain(wide, expiry=_EXPIRY, now=_NOW, side="bid")
    hi = build_chain(wide, expiry=_EXPIRY, now=_NOW, side="ask")
    band_ok = lo.forward < chain.forward < hi.forward
    print(f"  [{'PASS' if band_ok else 'FAIL'}] forward band {lo.forward:,.0f} < "
          f"{chain.forward:,.0f} < {hi.forward:,.0f}")
    ok &= band_ok

    print("\n9. degenerate inputs don't explode")
    empty = build_chain([], expiry=_EXPIRY, now=_NOW)
    print(f"  [{'PASS' if not empty.rows else 'FAIL'}] empty input -> empty chain, "
          f"warnings: {empty.warnings}")
    ok &= not empty.rows
    expired = build_chain(_lognormal_digitals(coarse), expiry=_NOW - timedelta(days=1), now=_NOW)
    no_iv = all(r.iv is None for r in expired.rows)
    print(f"  [{'PASS' if no_iv else 'FAIL'}] past expiry -> IVs suppressed")
    ok &= no_iv

    print("\n10. regression: a wide bin ladder must not smear mass into the wings")
    # Reproduces the live KXBTC failure: ~190 bins, all but two quoted 0.00/0.01.
    # Naive midpoint cumulation adds ~190 * 0.005 = 0.95 of fictitious mass and
    # pushed the implied forward thousands of dollars off spot.
    ladder = []
    for i in range(190):
        lo_k = 53_700 + 100 * i
        real = {62_700: (0.58, 0.64), 62_800: (0.32, 0.39)}.get(lo_k)
        bid, ask = real if real else (0.00, 0.01)
        ladder.append({"ticker": f"B{lo_k}", "strike_type": "between",
                       "floor_strike": lo_k, "cap_strike": lo_k + 99.99,
                       "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}"})
    warns: list[str] = []
    ds = digitals_from_markets(ladder, warnings=warns)
    lc = build_chain(ds, expiry=_EXPIRY, now=_NOW)
    print(f"         raw midpoints would have summed to {188 * 0.005 + 0.61 + 0.355:.2f}, not 1.00")
    ok &= _check("forward lands on the traded bins", lc.forward, 62_800, 0.01)
    mass_in_traded = sum(r.pdf for r in lc.rows if 62_600 <= r.strike <= 62_900) * 100
    ok &= _check("mass concentrated in the two live bins", mass_in_traded, 1.0, 0.05)
    print(f"  [{'PASS' if warns else 'FAIL'}] renormalisation was surfaced: {warns}")
    ok &= bool(warns)

    print("\n11. regression: a 1c quote floor on the dead wings must not inflate calls")
    # Reproduces the live KXBTCD failure. The ladder runs ~8 sigma each way, so
    # most of it is pinned at 0.00/0.01 (or 0.99/1.00 on the low side). Those
    # midpoints are tick artefacts, but D(K) gets *integrated*, so the dead wing
    # adds a constant to every call -- which reads back as a huge fake smile
    # while leaving the forward almost untouched, because the two wings' errors
    # cancel there. Symptom: cdf/pdf imply one vol, the call column another.
    wide = [_F0 - 25_000 + 250 * i for i in range(201)]
    warns11: list[str] = []
    kept = digitals_from_markets(_lognormal_threshold_markets(wide), warnings=warns11)
    raw = digitals_from_markets(_lognormal_threshold_markets(wide), drop_pinned=False)
    good = build_chain(kept, expiry=_EXPIRY, now=_NOW)
    bad = build_chain(raw, expiry=_EXPIRY, now=_NOW)
    print(f"         {len(raw)} quoted strikes -> {len(kept)} informative")
    print(f"         unfiltered: forward {bad.forward:,.0f}, ATM IV {bad.atm().iv * 100:.1f}%"
          f"  (forward looks fine; the vol does not)")
    ok &= _check("filtered forward", good.forward, _F0, 2e-3)
    ok &= _check("filtered ATM IV", good.atm().iv, _SIGMA, 0.02)

    # Beyond ~1.5 sigma the top live book is one tick wide (0.01/0.02, so +/-50%
    # on s_last) and no tail model can beat that. What must hold is that those
    # rows say so, and that the bracket still contains the truth.
    row = min(good.rows, key=lambda r: abs(r.strike - (_F0 + 2_500)))
    ok &= _check(f"filtered call K={row.strike:,.0f} (quote-driven)",
                 row.call, _black76_call(_F0, row.strike, _TAU, _SIGMA, 1.0), 0.05)
    # tail_weight is meant to say where the extrapolation, not the book, is
    # driving the number -- so it should dominate the relative error it can be
    # introducing. Slack covers tick noise inside the quoted range.
    brack = bounded = True
    for r in good.rows:
        want = _black76_call(_F0, r.strike, _TAU, _SIGMA, 1.0)
        if not (r.call_lo - 1e-6 <= want <= r.call_hi + 1e-6):
            brack = False
            print(f"  [FAIL] K={r.strike:,.0f}: {want:,.2f} outside "
                  f"[{r.call_lo:,.2f}, {r.call_hi:,.2f}]")
        rel = abs(r.call - want) / max(want, 1e-9)
        if rel > r.tail_weight / max(1.0 - r.tail_weight, 1e-9) + 0.02:
            bounded = False
            print(f"  [FAIL] K={r.strike:,.0f}: off {rel:.1%} but tail_weight only "
                  f"{r.tail_weight:.3f}")
    print(f"  [{'PASS' if brack else 'FAIL'}] [lo,hi] brackets truth at all "
          f"{len(good.rows)} strikes")
    print(f"  [{'PASS' if bounded else 'FAIL'}] tail_weight bounds the relative call "
          f"error at every strike")
    ok &= brack and bounded

    # The test has to bite: confirm the unfiltered chain really is broken.
    bw = min(bad.rows, key=lambda r: abs(r.strike - (_F0 + 5_000)))
    gw = min(good.rows, key=lambda r: abs(r.strike - (_F0 + 5_000)))
    tw = _black76_call(_F0, bw.strike, _TAU, _SIGMA, 1.0)
    bites = bw.call > 1.5 * tw and bw.iv - good.atm().iv > 0.05
    print(f"  [{'PASS' if bites else 'FAIL'}] unfiltered chain is visibly wrong at "
          f"K={bw.strike:,.0f}: call {bw.call:,.2f} vs true {tw:,.2f} "
          f"(filtered {gw.call:,.2f}), fake smile {bw.iv * 100:.1f}% vs ATM "
          f"{bad.atm().iv * 100:.1f}%")
    ok &= bites
    print(f"  [{'PASS' if warns11 else 'FAIL'}] drop was surfaced: {warns11}")
    ok &= bool(warns11)

    # Bins are a partition and must survive untouched: dropping members would
    # break contiguity and send `_from_bins` down its raw-midpoint fallback.
    bin_warns: list[str] = []
    dead = [{"ticker": f"z{i}", "strike_type": "between",
             "floor_strike": 90_000 + 100 * i, "cap_strike": 90_000 + 100 * i + 99.99,
             "yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0100"} for i in range(50)]
    kept_bins = digitals_from_markets(dead, warnings=bin_warns)
    print(f"  [{'PASS' if len(kept_bins) == 50 else 'FAIL'}] "
          f"bin ladder untouched by the filter ({len(kept_bins)}/50 kept)")
    ok &= len(kept_bins) == 50

    print("\n12. SVG output is well-formed and carries every row")
    import xml.etree.ElementTree as ET
    svg = cc.format_svg(event="KXBTCD-SELFTEST")
    parsed = True
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        parsed = False
        print(f"  [FAIL] not well-formed XML: {exc}")
    print(f"  [{'PASS' if parsed else 'FAIL'}] parses as XML ({len(svg):,} bytes)")
    ok &= parsed
    if parsed:
        body = "".join(root.itertext())
        missing = [r.strike for r in cc.rows if f"{r.strike:,.0f}" not in body]
        print(f"  [{'PASS' if not missing else 'FAIL'}] all {len(cc.rows)} strikes present"
              + (f", missing {missing}" if missing else ""))
        ok &= not missing
        # A dumb rasteriser ignores <style>; presentation attributes must stand alone.
        texts = root.findall(".//{http://www.w3.org/2000/svg}text")
        unfilled = [t for t in texts if not t.get("fill")]
        print(f"  [{'PASS' if not unfilled else 'FAIL'}] every text node carries a "
              f"fallback fill ({len(texts)} nodes)")
        ok &= not unfilled
        themed = "prefers-color-scheme" in svg
        print(f"  [{'PASS' if themed else 'FAIL'}] dark mode declared")
        ok &= themed

    print("\n13. payoff SVG: the stacks it draws really do bracket the call")
    # The figure claims two things. Check them as arithmetic first, on the same
    # numbers the drawing code uses, then check the drawing parses.
    ai = cc.rows.index(cc.atm())
    kk = [r.strike for r in cc.rows]
    sv = [1.0 - r.cdf for r in cc.rows]
    katm = kk[ai]
    sup_cost = sum((kk[i + 1] - kk[i]) * sv[i] for i in range(ai, len(kk) - 1))
    sub_cost = sum((kk[i + 1] - kk[i]) * sv[i + 1] for i in range(ai, len(kk) - 1))
    tail_area = cc.rows[-1].call / cc.discount
    ok &= _check("sub-replicating stack costs call_lo", sub_cost,
                 cc.rows[ai].call_lo / cc.discount, 1e-9)
    ok &= _check("super-replicating stack + tail costs call_hi", sup_cost + tail_area,
                 cc.rows[ai].call_hi / cc.discount, 1e-9)

    # ... and that the stacks really sandwich the payoff, inside the ladder.
    sandwich = True
    for s in [kk[0] + (kk[-1] - kk[0]) * i / 40.0 for i in range(41)]:
        sup = sum((kk[i + 1] - kk[i]) for i in range(ai, len(kk) - 1) if kk[i] < s)
        sub = sum((kk[i + 1] - kk[i]) for i in range(ai, len(kk) - 1) if kk[i + 1] < s)
        true = max(s - katm, 0.0)
        if not (sub - 1e-9 <= true <= sup + 1e-9):
            sandwich = False
            print(f"  [FAIL] S={s:,.0f}: {true:,.2f} outside [{sub:,.2f}, {sup:,.2f}]")
    print(f"  [{'PASS' if sandwich else 'FAIL'}] sub <= (S-K)+ <= super at 41 settlements")
    ok &= sandwich

    psvg = cc.format_payoff_svg(event="KXBTCD-SELFTEST")
    pparsed = True
    try:
        proot = ET.fromstring(psvg)
    except ET.ParseError as exc:
        pparsed = False
        print(f"  [FAIL] not well-formed XML: {exc}")
    print(f"  [{'PASS' if pparsed else 'FAIL'}] parses as XML ({len(psvg):,} bytes)")
    ok &= pparsed
    if pparsed:
        ptexts = proot.findall(".//{http://www.w3.org/2000/svg}text")
        punfilled = [t for t in ptexts if not t.get("fill")]
        print(f"  [{'PASS' if not punfilled else 'FAIL'}] every text node carries a "
              f"fallback fill ({len(ptexts)} nodes)")
        ok &= not punfilled
        pbody = "".join(proot.itertext())
        labelled = all(s in pbody for s in ("payoff at expiry", "D(K)", f"{katm:,.0f}"))
        print(f"  [{'PASS' if labelled else 'FAIL'}] both panels titled and the ATM "
              f"strike labelled")
        ok &= labelled
        pthemed = "prefers-color-scheme" in psvg
        print(f"  [{'PASS' if pthemed else 'FAIL'}] dark mode declared")
        ok &= pthemed
        # Nothing may spill past the canvas: geometry is hand-computed here.
        vb = [float(v) for v in proot.get("viewBox").split()]
        pts = []
        for tag in ("polyline", "polygon"):
            for el in proot.findall(f".//{{http://www.w3.org/2000/svg}}{tag}"):
                pts += [tuple(float(c) for c in p.split(","))
                        for p in el.get("points").split()]
        inside = all(-1 <= x <= vb[2] + 1 and -1 <= y <= vb[3] + 1 for x, y in pts)
        print(f"  [{'PASS' if inside else 'FAIL'}] all {len(pts)} plotted points inside "
              f"the {vb[2]:.0f}x{vb[3]:.0f} canvas")
        ok &= inside
        ok &= _slabs_tile(proot, len(cc.rows) - 1 - ai, "call")

    stub = build_chain(_lognormal_digitals(coarse[:2]), expiry=_EXPIRY, now=_NOW,
                       min_strikes=2).format_payoff_svg()
    stub_ok = "does not have two of them" in stub and "2 live strikes" in stub
    try:
        ET.fromstring(stub)
    except ET.ParseError:
        stub_ok = False
    print(f"  [{'PASS' if stub_ok else 'FAIL'}] too-short ladder renders an honest stub")
    ok &= stub_ok

    print("\n14. put payoff SVG: the digital-put stacks really do bracket the put")
    # Same two claims as 13, mirrored: the stacks below the strike price out to
    # put_lo / put_hi, and they sandwich (K - S_T)+ at settlement.
    fq = [r.cdf for r in cc.rows]
    below = cc.rows[0].put / cc.discount          # int_0^K0 F(u) du, by definition
    sub_cost = sum((kk[i + 1] - kk[i]) * fq[i] for i in range(ai))
    sup_cost = sum((kk[i + 1] - kk[i]) * fq[i + 1] for i in range(ai))
    trapz = sum(0.5 * (fq[i] + fq[i + 1]) * (kk[i + 1] - kk[i]) for i in range(ai))
    ok &= _check("trapezoid on F + below-ladder area == the chain's put",
                 trapz + below, cc.rows[ai].put / cc.discount, 1e-9)
    brackets_put = sub_cost <= cc.rows[ai].put / cc.discount <= sup_cost + below + 1e-9
    print(f"  [{'PASS' if brackets_put else 'FAIL'}] put_lo {sub_cost:,.2f} <= put "
          f"{cc.rows[ai].put / cc.discount:,.2f} <= put_hi {sup_cost + below:,.2f}")
    ok &= brackets_put

    psandwich = True
    for s in [kk[0] + (kk[-1] - kk[0]) * i / 40.0 for i in range(41)]:
        sup = sum((kk[i + 1] - kk[i]) for i in range(ai) if kk[i + 1] >= s)
        sub = sum((kk[i + 1] - kk[i]) for i in range(ai) if kk[i] >= s)
        true = max(katm - s, 0.0)
        if not (sub - 1e-9 <= true <= sup + 1e-9):
            psandwich = False
            print(f"  [FAIL] S={s:,.0f}: {true:,.2f} outside [{sub:,.2f}, {sup:,.2f}]")
    print(f"  [{'PASS' if psandwich else 'FAIL'}] sub <= (K-S)+ <= super at 41 settlements")
    ok &= psandwich

    qsvg = cc.format_put_payoff_svg(event="KXBTCD-SELFTEST")
    qparsed = True
    try:
        qroot = ET.fromstring(qsvg)
    except ET.ParseError as exc:
        qparsed = False
        print(f"  [FAIL] not well-formed XML: {exc}")
    print(f"  [{'PASS' if qparsed else 'FAIL'}] parses as XML ({len(qsvg):,} bytes)")
    ok &= qparsed
    if qparsed:
        qtexts = qroot.findall(".//{http://www.w3.org/2000/svg}text")
        qunfilled = [t for t in qtexts if not t.get("fill")]
        print(f"  [{'PASS' if not qunfilled else 'FAIL'}] every text node carries a "
              f"fallback fill ({len(qtexts)} nodes)")
        ok &= not qunfilled
        qbody = "".join(qroot.itertext())
        qlabelled = all(s in qbody for s in ("payoff at expiry", "F(K)", f"{katm:,.0f}"))
        print(f"  [{'PASS' if qlabelled else 'FAIL'}] both panels titled and the ATM "
              f"strike labelled")
        ok &= qlabelled
        qthemed = "prefers-color-scheme" in qsvg
        print(f"  [{'PASS' if qthemed else 'FAIL'}] dark mode declared")
        ok &= qthemed
        vb = [float(v) for v in qroot.get("viewBox").split()]
        qpts = []
        for tag in ("polyline", "polygon"):
            for el in qroot.findall(f".//{{http://www.w3.org/2000/svg}}{tag}"):
                qpts += [tuple(float(c) for c in p.split(","))
                         for p in el.get("points").split()]
        qinside = all(-1 <= x <= vb[2] + 1 and -1 <= y <= vb[3] + 1 for x, y in qpts)
        print(f"  [{'PASS' if qinside else 'FAIL'}] all {len(qpts)} plotted points inside "
              f"the {vb[2]:.0f}x{vb[3]:.0f} canvas")
        ok &= qinside
        ok &= _slabs_tile(qroot, ai, "put")

    qstub = build_chain(_lognormal_digitals(coarse[:2]), expiry=_EXPIRY, now=_NOW,
                        min_strikes=2).format_put_payoff_svg()
    qstub_ok = "does not have two of them" in qstub and "2 live strikes" in qstub
    try:
        ET.fromstring(qstub)
    except ET.ParseError:
        qstub_ok = False
    print(f"  [{'PASS' if qstub_ok else 'FAIL'}] too-short ladder renders an honest stub")
    ok &= qstub_ok

    print("\n15. density-vs-call consistency flags a smeared ladder, not a clean one")
    # The live bin ladders fail this way: the shape near the money is right, but
    # a few percent of mass sits on strikes across a +/-15% ladder that are not
    # really quoted, and integrating it lifts the whole call column. Build that
    # failure directly -- a lognormal with a uniform component mixed in.
    smear_ks = [_F0 - 25_000 + 250 * i for i in range(201)]
    lo_k, hi_k = smear_ks[0], smear_ks[-1]

    def _smeared(weight: float) -> OptionChain:
        ds = []
        for k, d in zip(smear_ks, _lognormal_digitals(smear_ks)):
            ramp = max(0.0, min(1.0, (hi_k - k) / (hi_k - lo_k)))
            s = (1.0 - weight) * d.mid + weight * ramp
            ds.append(Digital(strike=k, bid=s, ask=s))
        return build_chain(ds, expiry=_EXPIRY, now=_NOW)

    clean = _smeared(0.0)
    quiet = _vol_consistency(clean.rows, clean.forward, clean.tau) is None
    print(f"  [{'PASS' if quiet else 'FAIL'}] silent on an exact lognormal "
          f"(ATM iv {clean.atm().iv * 100:.1f}%, density "
          f"{_density_vol(clean.rows, clean.forward, clean.tau) * 100:.1f}%)")
    ok &= quiet

    dirty = _smeared(0.10)
    fires = _vol_consistency(dirty.rows, dirty.forward, dirty.tau)
    print(f"  [{'PASS' if fires else 'FAIL'}] fires on 10% smeared mass "
          f"(ATM iv {dirty.atm().iv * 100:.1f}%, density "
          f"{_density_vol(dirty.rows, dirty.forward, dirty.tau) * 100:.1f}%)")
    ok &= bool(fires)
    # ... and the forward stays put through all of it, which is the point: an
    # agreeing forward is not evidence that the call column is sound.
    ok &= _check("forward barely moves under the smear", dirty.forward, clean.forward, 2e-3)

    # The constant-offset failure is a different animal and is NOT this check's
    # job; assert that explicitly so nobody reads silence as coverage.
    pin_ratio = bad.atm().iv / _density_vol(bad.rows, bad.forward, bad.tau)
    print(f"  [INFO] pinned-quote chain from step 11 sits at {pin_ratio:.2f}x -- under "
          f"the {_VOL_GAP} threshold; `_pinned` and tail_weight own that case")

    print("\n16. greeks: the fitted four against finite differences, the exact two "
          "against the ladder")
    # Tier 1 -- Black-76 partials. Differentiate the closed form numerically and
    # the analytic greeks must land on it; that is the only thing these four
    # claim to be. Done off-ATM and at a non-zero rate so theta's carry term is
    # actually exercised rather than cancelling.
    gf, gk, gr = _F0, _F0 * 1.05, 0.05
    gdf = math.exp(-gr * _TAU)

    def _px(f: float, k: float, t: float, s: float, kind: str) -> float:
        d = math.exp(-gr * t)
        c = _black76_call(f, k, t, s, d)
        return c if kind == "call" else c - d * (f - k)   # parity, exactly

    for kind in ("call", "put"):
        dl, gm, vg, th = _black76_greeks(gf, gk, _TAU, _SIGMA, gdf, kind)
        hf, hs, ht = gf * 1e-5, 1e-6, 1e-8
        fd_dl = (_px(gf + hf, gk, _TAU, _SIGMA, kind)
                 - _px(gf - hf, gk, _TAU, _SIGMA, kind)) / (2 * hf)
        fd_gm = (_px(gf + hf, gk, _TAU, _SIGMA, kind) - 2 * _px(gf, gk, _TAU, _SIGMA, kind)
                 + _px(gf - hf, gk, _TAU, _SIGMA, kind)) / (hf * hf)
        fd_vg = (_px(gf, gk, _TAU, _SIGMA + hs, kind)
                 - _px(gf, gk, _TAU, _SIGMA - hs, kind)) / (2 * hs) / 100.0
        fd_th = -(_px(gf, gk, _TAU + ht, _SIGMA, kind)
                  - _px(gf, gk, _TAU - ht, _SIGMA, kind)) / (2 * ht) / 365.0
        ok &= _check(f"{kind} delta vs dV/dF", dl, fd_dl, 1e-6)
        ok &= _check(f"{kind} gamma vs d2V/dF2", gm * 1e6, fd_gm * 1e6, 1e-4)
        ok &= _check(f"{kind} vega vs dV/dsigma", vg, fd_vg, 1e-6)
        ok &= _check(f"{kind} theta vs -dV/dt", th, fd_th, 1e-5)

    # Parity ties the two sides together: C - P = DF(F - K) is linear in F and
    # flat in sigma, so the deltas must differ by exactly the discount factor and
    # the vegas not at all.
    cd, cg, cv, ct = _black76_greeks(gf, gk, _TAU, _SIGMA, gdf, "call")
    pd_, pg, pv, pt = _black76_greeks(gf, gk, _TAU, _SIGMA, gdf, "put")
    ok &= _check("delta_call - delta_put == DF", cd - pd_, gdf, 1e-12)
    ok &= _check("vega_call - vega_put == 0", cv - pv, 0.0, 1e-12)
    ok &= _check("gamma_call - gamma_put == 0", cg - pg, 0.0, 1e-12)
    ok &= _check("theta_call - theta_put == r(C-P) per day",
                 (ct - pt) * 365.0,
                 gr * (_px(gf, gk, _TAU, _SIGMA, "call") - _px(gf, gk, _TAU, _SIGMA, "put")),
                 1e-9)

    # Tier 2 -- the strike derivatives, which are not fitted to anything. These
    # must come back as the quote and the density themselves, and their parity is
    # dC/dK - dP/dK = -DF, since C - P = DF(F - K) differentiates to -DF.
    gch = build_chain(_lognormal_digitals(strikes), expiry=_EXPIRY, now=_NOW)
    grow = gch.atm()
    gc, gp = gch.greeks(grow, "call"), gch.greeks(grow, "put")
    ok &= _check("dC/dK == -DF x the digital quote", gc.dual_delta,
                 -gch.discount * (1.0 - grow.cdf), 1e-12)
    ok &= _check("dP/dK == +DF x Q(S <= K)", gp.dual_delta,
                 gch.discount * grow.cdf, 1e-12)
    ok &= _check("d2C/dK2 == DF x the density", gc.dual_gamma,
                 gch.discount * grow.pdf, 1e-12)
    ok &= _check("dC/dK - dP/dK == -DF", gc.dual_delta - gp.dual_delta, -gch.discount, 1e-12)

    # And the claim that makes them worth printing: they really are the slope of
    # the reconstructed call column, not just of the model that column is fitted
    # to. Central difference across the neighbouring strikes of the chain itself.
    gi = gch.rows.index(grow)
    k_lo, k_hi = gch.rows[gi - 1], gch.rows[gi + 1]
    ok &= _check("dC/dK matches the call column's own slope",
                 (k_hi.call - k_lo.call) / (k_hi.strike - k_lo.strike),
                 gc.dual_delta, 5e-3)
    ok &= _check("dP/dK matches the put column's own slope",
                 (k_hi.put - k_lo.put) / (k_hi.strike - k_lo.strike),
                 gp.dual_delta, 5e-3)

    # A row whose vol inversion failed still has exact strike derivatives -- the
    # figures print those and dash the rest, so None must survive the trip.
    noiv = replace(grow, iv=None)
    gnone = gch.greeks(noiv, "call")
    intact = (gnone.delta is None and gnone.gamma is None and gnone.vega is None
              and gnone.theta is None and gnone.dual_delta == gc.dual_delta)
    print(f"  [{'PASS' if intact else 'FAIL'}] a row with no IV keeps dC/dK and "
          f"dashes the Black-76 four")
    ok &= intact
    dashes = sum(1 for _, v, _ in _greek_cells(gnone) if v == "--")
    ok &= dashes == 4
    print(f"  [{'PASS' if dashes == 4 else 'FAIL'}] the strip dashes exactly those "
          f"four cells ({dashes} of 4)")

    # Finally, the numbers have to reach the figures.
    for label, svg, sym in (("call", gch.format_payoff_svg(), "dC/dK"),
                            ("put", gch.format_put_payoff_svg(), "dP/dK")):
        root = ET.fromstring(svg)
        cells = [t.text for t in root.iter("{http://www.w3.org/2000/svg}text")]
        want = [f"delta  d{sym[1]}/dF", "gamma  x1e6", "vega  per vol pt",
                "theta  per day", f"{sym}  off the ladder"]
        drawn = all(lab in cells for lab in want)
        exact = f"{gch.greeks(gch.atm(), label).dual_delta:.4f}" in cells
        print(f"  [{'PASS' if drawn and exact else 'FAIL'}] {label}.svg carries all "
              f"five greeks, with {sym} printed to 4dp")
        ok &= drawn and exact

    print("\n17. the payoff figures carry every banded strike, not just the ATM row")
    for label, svg, sym in (("call", gch.format_payoff_svg(), "dC/dK"),
                            ("put", gch.format_put_payoff_svg(), "dP/dK")):
        root = ET.fromstring(svg)
        cells = [t.text for t in root.iter("{http://www.w3.org/2000/svg}text")]
        heads = ["strike", "D(K)", "cdf", "pdf x1e6", label, "iv", "delta",
                 "gamma x1e6", "vega", "theta", sym]
        got_heads = all(hd in cells for hd in heads)
        print(f"  [{'PASS' if got_heads else 'FAIL'}] {label}.svg heads all "
              f"{len(heads)} columns")
        ok &= got_heads

        banded = [r for r in gch.rows if _TABLE_BAND <= r.cdf <= 1.0 - _TABLE_BAND]
        rows_drawn = all(f"{r.strike:,.0f}" in cells for r in banded)
        print(f"  [{'PASS' if rows_drawn else 'FAIL'}] all {len(banded)} banded "
              f"strikes have a row (of {len(gch.rows)} quoted)")
        ok &= rows_drawn

        # The exactness claim, made checkable in the drawing itself: the strike
        # derivative column has to reproduce the quote column it is derived from,
        # negated for the call and as-is for the put, on every single row.
        want = [(f"{-(1.0 - r.cdf):.3f}" if label == "call" else f"{r.cdf:.3f}")
                for r in banded]
        agrees = all(v in cells for v in want)
        print(f"  [{'PASS' if agrees else 'FAIL'}] every {sym} cell equals "
              f"{'-D(K)' if label == 'call' else 'Q(S <= K)'} on the same row")
        ok &= agrees

        # Values worth printing have to be the chain's own, not re-derived.
        vals = [f"{(r.call if label == 'call' else r.put):,.2f}" for r in banded
                if not r.model_dependent]
        carried = all(v in cells for v in vals)
        print(f"  [{'PASS' if carried else 'FAIL'}] every {label} value matches "
              f"the chain's own column")
        ok &= carried

    # Parity across the two figures' tables, read off the rendered numbers.
    band = [r for r in gch.rows if _TABLE_BAND <= r.cdf <= 1.0 - _TABLE_BAND]
    worst = max(abs((r.call - r.put) - gch.discount * (gch.forward - r.strike))
                for r in band)
    ok &= _check("call - put == DF(F - K) across every banded row", worst, 0.0, 1e-6)

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


# --------------------------------------------------------------------------
# interactive front end
# --------------------------------------------------------------------------
# One flow, no flags: pick a coin, pick an expiry shown in your own timezone,
# and get the chain table plus both payoff figures for the strike nearest the
# forward.  Everything the old flags exposed is fixed at the defaults that were
# already right for these markets -- mid quotes, exponential tails, zero carry.

_CONFIG = "config.ini"
_MAX_EVENTS = 24            # expiries offered per coin

# Only threshold ladders ("above/below") reach the menu.  `digitals_from_markets`
# can also integrate a bin ladder ("range"), and still will if you hand it one,
# but a bin pays only inside its own floor/cap, so reconstructing D(K) from it
# means cumulating ~190 quotes and every dead bin contributes a sliver of mass
# the ladder cannot shed -- `_pinned` may not drop bins without breaking the
# partition.  That is the wrong payoff to build vanillas out of: the density
# comes out roughly right near the money while the call column, which integrates
# the whole tail, does not.  The rest of the Crypto category -- FDV one-offs,
# annual min/max, 15-minute up/down binaries -- has no strike ladder behind it,
# so it never reaches the menu either.  The hourly/daily filter is the same
# assumption `rate=0` rests on: carry over a few hours or days is far inside the
# tick, which is not true of the annual series.
_LADDER_TITLE = ("above/below",)
_LADDER_FREQ = {"hourly", "daily"}

# Shown first, in this order; everything else follows alphabetically.
_MAJORS = ("Bitcoin", "Ethereum", "Solana", "XRP", "Dogecoin")

# The exchange spells one coin several ways across series -- the ticker symbol in
# one title and the full name in another. Same underlying, so they belong on one
# menu line rather than two.
_ALIAS = {"sol": "Solana", "ripple": "XRP", "btc": "Bitcoin", "eth": "Ethereum"}

# If /series is unreachable the majors still work.  Solana has no entry because
# the only Solana ladder these fallbacks ever named was a bin ladder; if it lists
# a threshold series, /series will find it.
_FALLBACK_SERIES = {
    "Bitcoin": ["KXBTCD"],
    "Ethereum": ["KXETHD"],
    "XRP": ["KXXRPD"],
    "Dogecoin": ["KXDOGED"],
}

_FIGURES = ("chain.svg", "call.svg", "put.svg")


def _currency_of(title: str) -> str:
    """'Bitcoin price Above/below' -> 'Bitcoin'; '' when no coin is named."""
    s = title.strip()
    low = s.lower()
    for phrase in ("price above/below", "above/below", "price range", "range", "price"):
        i = low.find(phrase)
        if i >= 0:
            s = s[:i]
            break
    return " ".join(s.split())


def _ladder_catalogue(client) -> dict[str, list[str]]:
    """coin -> [series ticker], for the threshold ladders only."""
    try:
        raw = client.series(category="Crypto")
    except Exception as exc:                      # noqa: BLE001 - offline is not fatal
        print(f"  (series listing unavailable: {exc}; falling back to the majors)")
        return dict(_FALLBACK_SERIES)

    # Grouped case-insensitively -- "Bitcoin Cash" and "Bitcoin cash" are one
    # coin -- and displayed under whichever spelling sorts first, which is the
    # capitalised one.
    tickers: dict[str, list[str]] = defaultdict(list)
    spellings: dict[str, set[str]] = defaultdict(set)
    for s in raw:
        title = str(s.get("title") or "")
        low = title.lower()
        if s.get("frequency") not in _LADDER_FREQ or "up down" in low:
            continue
        if not any(p in low for p in _LADDER_TITLE):
            continue
        coin = _currency_of(title)
        if not coin:
            continue
        coin = _ALIAS.get(coin.casefold(), coin)
        key = coin.casefold()
        spellings[key].add(coin)
        ticker = str(s.get("ticker"))
        if ticker not in tickers[key]:
            tickers[key].append(ticker)
    out = {min(spellings[k]): v for k, v in tickers.items()}
    return out or dict(_FALLBACK_SERIES)


def _until(when: datetime, now: datetime) -> str:
    secs = (when - now).total_seconds()
    if secs <= 0:
        return "closed"
    mins = int(secs // 60)
    hours, mins = divmod(mins, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {mins:02d}m"
    return f"in {mins}m"


def _choose(what: str, labels: Sequence[str]) -> int | None:
    """Numbered menu. None when the user quits or the input dries up."""
    for i, label in enumerate(labels, 1):
        print(f"  {i:>2})  {label}")
    while True:
        try:
            raw = input(f"\nchoose {what} [1-{len(labels)}, q to quit]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw in ("q", "quit", "exit"):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(labels):
            return int(raw) - 1
        print("  not one of the numbers on the list")


def _open(paths: Sequence[str]) -> None:
    """Hand the figures to the desktop viewer, if there is one."""
    opener = shutil.which("xdg-open") or shutil.which("open")
    if not opener:
        return
    for p in paths:
        try:
            subprocess.Popen([opener, p], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            return


def main() -> int:
    print("\nriemann chain  ·  vanilla options out of Kalshi binaries")

    # Imported here, not at module scope, so the library half stays usable
    # without requests/cryptography installed.
    from kalshi_client import KalshiClient

    try:
        client = KalshiClient.from_config(_CONFIG)
    except Exception as exc:                      # noqa: BLE001 - report, don't traceback
        print(f"\ncould not read {_CONFIG}: {exc}")
        print("copy config.example.ini to config.ini and fill in your key,")
        print("or pick the offline selftest, which needs neither.")
        return 1

    catalogue = _ladder_catalogue(client)
    coins = sorted(catalogue, key=lambda c: (_MAJORS.index(c) if c in _MAJORS
                                             else len(_MAJORS), c))
    print("\ncrypto with a quoted strike ladder:\n")
    labels = [f"{c:<16}{len(catalogue[c])} ladder{'s' if len(catalogue[c]) > 1 else ''}"
              for c in coins]
    pick = _choose("a coin", labels + ["run the offline selftest  (no network)"])
    if pick is None:
        return 1
    if pick == len(coins):
        return selftest()
    coin = coins[pick]

    print(f"\nlooking up {coin} expiries ...")
    now = datetime.now(timezone.utc)
    dated: dict[str, tuple[datetime, str]] = {}
    for ticker in catalogue[coin]:
        try:
            found = client.events(series_ticker=ticker)
        except Exception as exc:                  # noqa: BLE001 - one dead series is survivable
            print(f"  {ticker}: {exc}")
            continue
        for ev in found:
            raw = ev.get("strike_date") or ev.get("close_time")
            event_ticker = str(ev.get("event_ticker") or "")
            if not raw or not event_ticker:
                continue
            try:
                when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if when > now:
                cadence = str((ev.get("product_metadata") or {}).get("cadence", ""))
                dated[event_ticker] = (when, cadence)

    if not dated:
        print(f"no open {coin} events with a settlement time")
        return 1

    # Soonest first. Each expiry appears once now that only threshold ladders are
    # listed: they quote D(K) directly rather than via a running sum over ~190
    # one-cent bins, so they are the far better conditioned half of what the
    # exchange offers on the same underlying and the same settlement time.
    order = sorted(dated, key=lambda t: (dated[t][0], t))
    order = order[:_MAX_EVENTS]
    # %Z is whatever the machine's zone is called; astimezone() with no argument
    # is what makes these local rather than UTC.
    print(f"\n{coin} expiries, your local time "
          f"({now.astimezone().strftime('%Z') or 'local'}):\n")
    pick = _choose("an expiry", [
        f"{dated[t][0].astimezone():%a %d %b  %H:%M}  {_until(dated[t][0], now):<11}"
        f"{dated[t][1]:<9}{t}"
        for t in order
    ])
    if pick is None:
        return 1
    event = order[pick]

    print(f"\nfetching {event} ...")
    ms = client.markets(event_ticker=event)
    if not ms:
        print("no open markets on that event")
        return 1

    chain = build_chain(ms)
    if not chain.rows:
        for w in chain.warnings:
            print(f"warning    {w}")
        print("not enough quoted strikes to reconstruct a chain")
        return 1

    print(f"\nevent      {event}   ({len(ms)} markets -> {len(chain.rows)} strikes)")
    if chain.expiry:
        print(f"expiry     {chain.expiry.astimezone():%Y-%m-%d %H:%M %Z}   "
              f"tau={chain.tau * 365 * 24:.2f}h")
    print(f"forward    {chain.forward:,.2f}")
    atm = chain.atm()
    if atm and atm.iv:
        print(f"ATM IV     {atm.iv * 100:.1f}%  (K={atm.strike:,.0f})")
    if chain.truncated_mass:
        print(f"tail mass  {chain.truncated_mass:.2%} outside quoted strikes")
    for w in chain.warnings:
        print(f"warning    {w}")
    print()

    # Strikes pinned at 0 or 1 carry no information -- they are just the ladder
    # running past where the market thinks the price can get to.
    shown = chain
    keep = [r for r in chain.rows if _TABLE_BAND <= r.cdf <= 1.0 - _TABLE_BAND]
    if keep:
        shown = replace(chain, rows=keep)
        hidden = len(chain.rows) - len(keep)
        if hidden:
            print(f"({hidden} strikes outside the {_TABLE_BAND:.0%}-"
                  f"{1 - _TABLE_BAND:.0%} CDF band hidden)")
    print(shown.format_table())

    # The payoff figures take the *full* chain, not the banded view: the stacks
    # that replicate the ATM option are held across every quoted strike, and
    # hiding the wings would move the "ladder ends here" line inward and
    # overstate the tail.
    table, call_fig, put_fig = _FIGURES
    for path, svg in ((table, shown.format_svg(event=event)),
                      (call_fig, chain.format_payoff_svg(event=event)),
                      (put_fig, chain.format_put_payoff_svg(event=event))):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(svg)
    print(f"\nwrote {', '.join(_FIGURES)}"
          + (f"   (ATM K={atm.strike:,.0f})" if atm else ""))
    # A thin ladder still tables and plots a survival curve, but it can leave one
    # side of the money with no stack to draw.  The file is written either way,
    # so say which one came back as a note instead of a figure.
    call_ok, put_ok = chain.replicable()
    stubbed = [f for f, ok in ((call_fig, call_ok), (put_fig, put_ok)) if not ok]
    if stubbed:
        print(f"{' and '.join(stubbed)} could not be drawn -- see the note in the "
              f"file. Only {len(chain.rows)} strike"
              f"{'' if len(chain.rows) == 1 else 's'} on this expiry survived the "
              f"extreme-tick filter; a longer-dated expiry quotes more.")
    _open(_FIGURES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
