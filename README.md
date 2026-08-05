# Synthetic Crypto Options Using Kalshi

*Vanilla calls, puts, implied vols and greeks, reconstructed from binary
prediction markets — no options exchange involved.*

Kalshi's crypto markets are cash-or-nothing **digital options**. A market paying
\$1 if BTC settles above K prices the risk-neutral survival probability

$$D(K) \;=\; \mathbb{Q}(S_T > K)$$

and vanilla options are the integral of that curve:

$$
\begin{aligned}
C(K) &= \mathbb{E}\big[(S_T-K)^+\big] &&= \int_K^{\infty} \mathbb{Q}(S_T > u)\,du \\[2pt]
P(K) &= \mathbb{E}\big[(K-S_T)^+\big] &&= \int_0^{K} \mathbb{Q}(S_T \le u)\,du \\[2pt]
F    &= \mathbb{E}[S_T]               &&= \int_0^{\infty} \mathbb{Q}(S_T > u)\,du
\end{aligned}
$$

That first line is the only step doing real work, and it is just the tail formula
$\mathbb{E}[X]=\int_0^{\infty}\mathbb{Q}(X>y)\,dy$ applied to the payoff itself:

$$
\mathbb{E}\big[(S_T-K)^+\big]
\;=\; \int_0^{\infty} \mathbb{Q}\big((S_T-K)^+ > y\big)\,dy
\;=\; \int_0^{\infty} \mathbb{Q}(S_T > K+y)\,dy
\;=\; \int_K^{\infty} \mathbb{Q}(S_T > u)\,du
$$

A vanilla *is* a stack of digitals, and the ladder quotes the integrand.

## The sum

So a Kalshi strike ladder already is a discretised call chain — all that is left
is to sum it. Write the quoted strikes $K_0 < \dots < K_{n-1}$, the digital levels
$D_i = D(K_i)$, the gaps $\Delta K_i = K_{i+1}-K_i$, and let $T$ be the modelled
mass above the top strike:

$$
\begin{aligned}
C_{\text{hi}}(K_j) &= \sum_{i=j}^{n-2} D_i\,\Delta K_i \;+\; T
  &&\text{left rule, plus the tail} \\[4pt]
C_{\text{lo}}(K_j) &= \sum_{i=j}^{n-2} D_{i+1}\,\Delta K_i
  &&\text{right rule, tail discarded} \\[4pt]
C^{\ast}(K_j)      &= \sum_{i=j}^{n-2} \tfrac{1}{2}\big(D_i + D_{i+1}\big)\Delta K_i \;+\; T
  &&\text{trapezoid — the point estimate} \\[4pt]
\text{band}        &= C_{\text{hi}} - C_{\text{lo}}
                    = \sum_{i=j}^{n-2} \big(D_i - D_{i+1}\big)\Delta K_i \;+\; T
\end{aligned}
$$

These are `call_hi`, `call_lo`, `call` and `band` in the output. $D$ is
non-increasing, so the left rule over-counts and the right rule under-counts and
the ladder brackets the call for free. $C_{\text{lo}}$ drops $T$ on purpose:
discarding mass above the top strike can only shrink $\mathbb{E}[(S_T-K)^+]$,
which makes it a bound that assumes nothing. There is no matching rigorous upper
bound, since unquoted mass can sit arbitrarily far out — so $C_{\text{hi}}$
carries the modelled tail and is an estimate. Everything else falls out of the
same curve:

$$
\begin{aligned}
F &= C^{\ast}(K_0) + L, \qquad L = \int_0^{K_0} D(u)\,du
  &&\text{the mass below the ladder} \\[4pt]
C(K_j) &= \mathrm{DF}\cdot C^{\ast}(K_j) \\[4pt]
P(K_j) &= C(K_j) - \mathrm{DF}\cdot\big(F - K_j\big)
  &&\text{parity, same curve} \\[4pt]
\mathrm{cdf}_i &= 1 - D_i \\[4pt]
f(K_i) &= \frac{D_{i-1} - D_{i+1}}{K_{i+1} - K_{i-1}}
  &&\text{one-sided at the ends}
\end{aligned}
$$

`build_chain` computes exactly these, cumulating downward from the top strike so
each `C(K_j)` reuses the sum above it, and hands back strikes, the implied CDF
and density, synthetic call/put values, the implied forward, Black-76 implied
vols and the greeks. Both $f$ and $P$ are floored at zero on the way out, which
is belt-and-braces rather than arithmetic: a PAVA-repaired $D$ is non-increasing,
so $f \ge 0$ already, and $P(K_0) = K_0 - L \ge 0$ because $L$ is an integral of
$D \le 1$ over $[0, K_0]$. If either floor ever binds, the curve reached the sum
in a state the repair was supposed to rule out.

## Setup

The reconstruction library and the selftest are pure standard library — clone and
run, nothing to install:

```bash
python3 -c "import riemann_chain; riemann_chain.selftest()"
```

Live quotes need the signed client and a Kalshi API key:

```bash
pip install -r requirements.txt
cp config.example.ini config.ini      # then fill in api_key_id
```

Kalshi shows the private key once, at creation; save it beside `config.ini` as
`kalshi_private_key.pem`. Both are gitignored. `use_demo = true` points at
demo-api.kalshi.co, so you can wire it up without touching a funded account —
and only GET endpoints are ever called, so a read-scoped key is enough.

## Use

```python
from riemann_chain import build_chain

chain = build_chain(markets)          # markets = /trade-api/v2/markets dicts
print(chain.forward, chain.atm_iv())
print(chain.format_table())
```

```bash
python3 riemann_chain.py
```

That is the whole interface — no flags. It asks which coin, lists that coin's open
expiries **in your local timezone**, and on your pick prints the chain and writes
three figures for the strike nearest the forward:

```
crypto with a quoted strike ladder:

   1)  Bitcoin         2 ladders
   2)  Ethereum        2 ladders
   ...
   9)  run the offline selftest  (no network)

choose a coin [1-9, q to quit]: 1

Bitcoin expiries, your local time (CDT):

   1)  Tue 04 Aug  01:00  in 46m     hourly   KXBTCD-26AUG0402
   2)  Tue 04 Aug  16:00  in 15h 46m daily    KXBTCD-26AUG0417
```

(The coins and counts come from a live `/series` call; yours will differ.)

The coin list is filtered to the hourly and daily **threshold** ladders —
*above/below* series, which quote `D(K)` directly. The 15-minute up/down
binaries, FDV one-offs and annual min/max markets have no strike ladder behind
them and never appear.

**Bin ladders (*range*) are deliberately not offered.** A bin pays only inside
its own floor/cap, which is the wrong payoff to rebuild vanillas from: `D(K)` has
to be cumulated across ~190 quotes, and `_pinned` — which drops tick-dead strikes
on a threshold ladder — cannot touch a bin ladder, because bins are a partition
and dropping members would break the contiguity and unit-mass constraints
`_from_bins` projects onto. The dead bins survive, keep slivers of mass, and get
integrated across a ladder running ±15%. On the same BTC daily expiry:

| | thresholds | bins |
|---|---|---|
| vol from the **density** | 32.2% | 31.4% |
| vol from the **call column** | 34.6% | **53.7%** |
| mass beyond ±5% of forward | 0.0000% | **4.76%** |
| implied forward | 63,831.81 | 63,555.04 |

Both see the same distribution near the money; the bin ladder's call column — the
one an option actually depends on — is inflated by mass on strikes that are not
really quoted. The library still reconstructs a bin ladder if you hand its
markets to `build_chain` yourself, renormalisation and all (see Validation); it
just no longer shows up as something to trade off.

The knobs the old flags exposed are gone rather than defaulted. Tail fitting and
PAVA repair are now unconditional — truncating the tail biases the forward and
every call low, and integrating a non-monotone `D(K)` produces negative
densities, so neither switch was ever worth throwing. The widest-usable-spread
cutoff is a module constant. What survives on `build_chain` is what gets used:
`side=` (the `bid`/`ask` chains that give the executable band), `rate=` (zero
carry is right for these expiries, not for every expiry), `min_strikes=`, and
`expiry=` / `now=` for pricing a ladder as of some moment other than now. The
extreme-tick filter is one level down, on `digitals_from_markets(...,
drop_pinned=False)`, since seeing what it drops means looking at the digitals
rather than the chain.

**`chain.svg`** is the table `OptionChain.format_svg()` renders: the same columns
plus an inline density bar, the ATM row marked, and the warnings carried into the
footer. It is dependency-free and self-contained — light and dark palettes, with
the light values also written as presentation attributes so a rasteriser that
ignores `<style>` still gets a correct render rather than black-on-black.

**`call.svg`** is `OptionChain.format_payoff_svg()` — the same reconstruction
drawn twice, once in price space and once in payoff space:

- **left**, the Riemann picture the module is named for: `D(K)` with the region
  right of the ATM strike shaded, since that area *is* `C(K_atm)`, and ruled at
  every strike into the `ΔK` columns the sum is made of. The two staircases
  straddling the curve are the left- and right-hand sums, i.e. `call_hi` and
  `call_lo`, and the dashed segment past the top strike is the modelled tail.
- **right**, those same two sums read at expiry. Holding `ΔK_i` digitals at each
  `K_i` at or above the strike super-replicates `(S_T − K)⁺`; the same widths struck
  one strike up sub-replicate it. **Each digital is drawn as its own slab** —
  `ΔK_m` high, running right from `K_m₊₁`, which is where and how much that
  contract pays — so the staircase is visibly the slabs piled on each other rather
  than an outline, and the height under any settlement is what the stack collects.
  The true hockey stick is trapped between the two, so the vertical gap here and
  the `band` column are the same quantity. Past the top quoted strike both stacks
  go flat and the payoff does not — that wedge is exactly what the tail model is
  guessing at, and why `call_lo` is a bound and `call_hi` is not.

It takes the *full* chain, not the CDF-banded view the table prints, so the
"ladder ends here" line sits where the quotes actually stop.

**`put.svg`** is the mirror image, `OptionChain.format_put_payoff_svg()`.
A put is the same sum walked in from the other end of the ladder — `P(K) = ∫₀ᴷ
Q(S_T ≤ u) du` — so the left panel plots the CDF and shades the area *left* of the
strike, and because `F` rises the staircases swap roles: the right-hand sum is
`put_hi`, the left-hand sum `put_lo`. On the right, `ΔK_i` digital *puts* struck at
each `K_i₊₁` below the strike super-replicate `(K − S_T)⁺` and the same widths one
strike down sub-replicate it, drawn as the same stacked slabs mirrored — `ΔK_m`
high, running left from `K_m`. Below the bottom quoted strike both stacks flatten at
`K_atm − K_0` while the payoff keeps climbing toward `K_atm`; that wedge is the
lower tail, and it is worth exactly the chain's put at the bottom strike, since
`P(K_0)` *is* the whole area below the ladder.

Both payoff figures carry a **greeks strip** under the price row, and it is split
on purpose. Differentiating
$C(K)=\mathrm{DF}\int_K^{\infty}\mathbb{Q}(S_T>u)\,du$ *in the strike* hands
back the integrand:

$$
\frac{\partial C}{\partial K} = -\,\mathrm{DF}\cdot\mathbb{Q}(S_T > K)
\qquad\qquad
\frac{\partial^2 C}{\partial K^2} = \mathrm{DF}\cdot f(K)
$$

which is Breeden–Litzenberger read backwards. Both sides of that are already on
the ladder — they are the `digital_mid` and `pdf` columns in different units — so
$\partial C/\partial K$ is **exact**, needs no vol, and stays right where the
smile is wrong. It is drawn in the digitals' own blue, and it is the slope of the
very curve the left panel plots. The put's mirror is
$\partial P/\partial K = +\mathrm{DF}\cdot\mathbb{Q}(S_T \le K)$, its own left
panel, and parity pins them:

$$\frac{\partial C}{\partial K} - \frac{\partial P}{\partial K} = -\,\mathrm{DF}$$

Delta, gamma, vega and theta differentiate in `F`, `σ` and `t` instead — none of
which one expiry's strike ladder pins down, since the quotes fix the
*distribution* of `S_T`, not how it responds when the forward or the vol moves.
Those four are Black-76 at that strike's own IV: a smile-local reading, exact
only for the option it was struck from, and dashed out entirely when the vol
inversion fails. `OptionChain.greeks(row, kind)` returns the lot.

Under the panels both figures print **every quoted strike in the 2%–98% CDF
band** — strike, `D(K)`, `cdf`, `pdf`, the reconstructed call or put, its `iv`,
and all five greeks per row, ATM marked. The panels argue about one strike; this
says what every other rung is worth, so the figure carries the whole chain
instead of a headline. The band is the same one the printed table uses, because
the *panels* deliberately take the full ladder (that is where the "quotes stop
here" line comes from) and a live ladder can run hundreds of strikes whose rows
would be all zeroes; the count hidden is stated under the table.

Reading down the `dC/dK` column against `D(K)` is the whole model-free claim in
one glance: they are the same numbers, negated. On the put side `dP/dK` and `cdf`
match outright.

**Both payoff figures need two live strikes on their own side of the money**, and
on these ladders that is a real constraint, not a formality. `_pinned` drops every
strike resting on the extreme tick, and on a short-dated crypto expiry the whole
distribution fits inside a few rungs — the ETH hourly ladder quotes 300 strikes
5 wide and, an hour out, exactly two of them are two-sided. When there is no stack
to draw, the figure writes a note giving the ladder's actual shape instead of a
picture, `main` says which figure fell back, and the chain table and `chain.svg`
are unaffected. A longer-dated expiry is the fix: on 2026-08-04 the same ETH
series went 5 live strikes at the 17:00 expiry to 14 three days out, and BTC 3 to
28. `OptionChain.replicable()` returns the two flags if you want to check first.

Live output (BTC daily, 112h to expiry):

```
forward    62,799.99      ATM IV 37.6% (K=63,000)

      strike    bid    mid    ask    cdf  pdf x1e6       call      +/-        put      iv
      59,000  0.910  0.925  0.940  0.075    40.000   3,970.00   462.50     170.00   45.4%
      61,000  0.760  0.770  0.780  0.230   120.000   2,253.75   385.00     453.75   40.6%
      63,000  0.510  0.515  0.520  0.485   170.000     970.00   257.50   1,170.00   37.6%
      65,000  0.170  0.175  0.180  0.825   115.000     322.50    87.50   2,522.50   37.7%
      67,000  0.050  0.055  0.060  0.945    25.000     127.50    27.50   4,327.50   42.4%
```

The smile falls from 45% to 37% and back to 42% — nothing in the code enforces
that shape; it comes out of independently quoted binaries.

## What the numbers mean

| field | meaning |
|---|---|
| `digital_bid/mid/ask` | the binary's own quote, normalised to `Q(S > K)` |
| `cdf`, `pdf` | implied distribution; `pdf` is per \$1 of strike |
| `call`, `put` | trapezoid reconstruction, `put` by parity off the same curve |
| `call_lo` | **rigorous** lower bound — right-Riemann, tail discarded |
| `call_hi` | upper *estimate* — left-Riemann + modelled tail |
| `tail_weight` | fraction of `call` coming from extrapolation; `model_dependent` flags >25% |
| `iv` | Black-76 vol implied by `call` against the implied forward |

`OptionChain.greeks(row, kind)` adds the sensitivities, in the units a trader
quotes them rather than the raw partials:

| field | meaning | model? |
|---|---|---|
| `dual_delta` | `∂V/∂K` — **exact**, minus the digital quote at `K` | none |
| `dual_gamma` | `∂²V/∂K²` — **exact**, `DF ×` the density | none |
| `delta` | `∂V/∂F`, per \$1 of forward | Black-76 at `row.iv` |
| `gamma` | `∂²V/∂F²`, per \$1² — displayed `× 1e6`, as `pdf` is | Black-76 at `row.iv` |
| `vega` | `∂V/∂σ`, per **vol point** | Black-76 at `row.iv` |
| `theta` | `−∂V/∂t`, per **day** | Black-76 at `row.iv` |

The Black-76 four are `None` when the vol inversion failed; the two strike
derivatives are always there, since they need only the quote and the density.
Theta's carry term uses the rate recovered from the chain's own discount factor,
so at the default `r = 0` it is pure gamma rent.

`side="bid"`/`"ask"` rebuild the whole chain from that side of the book, giving
an executable band around the mid.

## Things worth knowing

Most of the work here is not the integral — it's making the integral survive a
real order book.

- **Bin ladders must be renormalised**, which is half of why the menu no longer
  lists them. The hourly series (`KXBTC`) quotes ~190
  `between` bins, and all but two sit at 0.00 bid / 0.01 ask. Cumulating each
  bin's 0.005 midpoint injects ~0.95 of fictitious probability, which smears the
  survival curve and threw the implied forward 3.6% off spot. Because the bins
  partition the outcome space, their masses must sum to 1, so the midpoints are
  projected onto that constraint inside each bin's own bid/ask box — dead bins
  clip to their zero bid, traded bins keep their mass. Test 10 pins this.
- **Threshold ladders must have their dead wings dropped.** The same tick
  artefact bites the `greater`/`less` series (`KXBTCD`) a different way. A
  far-OTM strike quoted 0.00 / 0.01 has a 0.005 midpoint, and `D(K)` gets
  *integrated*: a 189-strike ladder running \$9,500 past the money at 100-wide
  strikes adds `0.005 × 9,500 = $47.50` to every call on the board. The forward
  survives — the upper wing's floor and the lower wing's 0.995 ceiling cancel —
  so the tell is that `cdf`/`pdf` imply one vol (~26%) and the `call` column
  another (43.5%), with a large symmetric smile that is really a constant. PAVA
  cannot see it, since a flat 0.005 is perfectly non-increasing. These strikes
  are dropped and the region left to the tail model. Test 11 pins this.
- **The tail fit needs a window that spans a real decay.** Out where the book
  trades for a cent or two, quantisation flattens `D(K)` into a staircase; a
  fixed 4-point log-linear fit lands on an exactly flat segment, gets `sxy = 0`,
  and silently degenerates to a one-grid-step tail — several times too small, and
  a constant of integration, so it drags down every call. `_upper_tail` widens
  its window until the curve has halved.
- **Use `close_time`, not `expiration_time`.** For these series `expiration_time`
  is a settlement deadline that can sit a week past the price observation.
  Reading it as expiry inflates τ by ~50× and guts the vols.
- **The tail is a model, not a bound.** Mass above the top strike can sit
  arbitrarily far out, so no rigorous upper bound exists. `call_lo` drops the
  tail and is therefore a real bound; `call_hi` carries it and is not.
- **Empty books are dropped, not midpointed.** A 0.00/1.00 quote has a 0.50
  midpoint that is pure fiction.
- **Crossed quotes get PAVA-repaired**, since `D(K)` must fall with `K` and a
  violation shows up downstream as negative density.
- Discounting defaults to zero — carry over a few hours is far inside the
  1-cent tick. Pass `rate=` if you disagree.

## Validation

The last entry on the coin menu (or `riemann_chain.selftest()`, whose number
depends on how many coins `/series` returned) prices binaries off a known
lognormal and
checks the reconstruction
inverts it, to 2e-3 on the forward, 1e-4 of notional on the calls, and 5e-3 on
implied vol across the 2–98% CDF band — the vol actually comes back to 4e-4 of
the 60% input, and the looser bound is there so tick-scale noise cannot fail the
suite. It also checks parity, the density integral, the lower bound, and the
degenerate cases. It runs offline — no API key, no network.

All three SVGs are checked too: well-formed XML, every strike present, a fallback
fill on every text node, dark mode declared, and nothing plotted outside the canvas.
The greeks are checked in both tiers. The Black-76 four are differentiated
numerically out of the closed form — off-ATM and at a non-zero rate, so theta's
carry term is exercised rather than cancelling — and the analytic values must
land on the finite differences; parity then pins `delta_call − delta_put = DF`,
equal gammas and vegas, and `theta_call − theta_put = r(C − P)`. The two strike
derivatives are checked against the quote and the density they are supposed to
be, against each other through `dC/dK − dP/dK = −DF`, and — the claim that makes
them worth printing — against the measured slope of the chain's own `call` and
`put` columns across neighbouring strikes. A row with no IV must keep its strike
derivatives and dash exactly the other four, in the object and in the strip.

The per-strike blocks are checked as data, not decoration: all 11 columns headed,
a row for every banded strike, every `dC/dK` cell equal to `-D(K)` on its own row
(and `dP/dK` to `cdf`), every printed option value equal to the chain's own
column, and `call - put = DF(F - K)` across every row of both tables.

The payoff figures get an extra arithmetic check, since a drawing that merely
*looks* like a replication is worth nothing — the stacks they draw are priced
independently and must come back as `call_lo`/`call_hi` to 1e-9 (and, on the put
side, must bracket the chain's own `put`, whose trapezoid identity is checked to
1e-9), and must sandwich `(S_T − K)⁺` and `(K − S_T)⁺` at every settlement sampled
across the ladder. The drawn slabs are checked too: one per digital in the stack,
tiling it in pixel space with no gaps and no overlaps, since a gap would be
drawing a payoff nobody holds.

The stronger check is external: `KXBTCD` (threshold ladder) and `KXBTC` (bin
ladder) are structurally independent markets on the same underlying and expiry.
Reconstructed separately — the bin side by passing `client.markets(...)` for a
`KXBTC` event straight to `build_chain`, since the menu no longer offers it —
their forwards agree to **0.05–0.14%**. Before the bin renormalisation fix they
disagreed by 3.6%.

Agreeing forwards are a weak check on their own, though: the pinned-quote bug
above left the forward almost exact while inflating every call ~70%, because the
two wings' errors cancel in `E[S]` and add in `E[(S−K)+]`. The sharper check is
that `cdf`/`pdf` and the `call` column must imply the *same* vol — the density is
a local derivative of `D(K)`, the call column an integral across the whole ladder,
so mass on strikes that are not really quoted moves one and not the other.

`_vol_consistency` now runs that comparison on every chain and appends a warning
when the ATM call implies more than **1.25×** the density's vol. Scope, measured
rather than assumed:

- **Catches** a call column scaled up wholesale — the bin-ladder failure, 1.8× on
  live BTC. Sensitivity floor is ~8–10% of mass smeared; below that the ratio sits
  inside the range real skew produces (threshold ladders run ~1.08).
- **Does not catch** tick-pinned quotes adding a *constant* to every call. A
  constant is small next to a fat ATM call (1.17× on the synthetic) and enormous
  next to a wing call (1.95× at 5% OTM), so it barely moves this ratio. `_pinned`
  drops those on the way in and `tail_weight` flags the remainder.

Step 15 of the selftest pins both directions: silent on an exact lognormal, firing
on 10% smeared mass, with the forward moving 3e-11 across the two — which is the
whole point, since an agreeing forward proves nothing about the call column.

## Layout

Two files.

- `riemann_chain.py` — the reconstruction, the three figures, plus the interactive
  front end and `selftest()`. The library half imports no third-party packages and
  touches no network; `main()` imports `kalshi_client` lazily, so
  `import riemann_chain` works on a box without `requests`/`cryptography`
  installed.
- `kalshi_client.py` — signed read-only REST client (RSA-PSS). `series()` and
  `events()` are what the menus are built from; `markets()` is only called once a
  single event has been chosen.

Reads `config.ini` for `api_key_id` / `private_key_path` / `use_demo`.
Only GETs market data; it never places an order.

## License

MIT — see [LICENSE](LICENSE).

Research code, not trading advice. The reconstruction is only as good as the
book behind it: every caveat under *Things worth knowing* is a way the numbers
go wrong on real quotes, and `call_hi` is an estimate rather than a bound. If you
put money behind it, that is on you.
