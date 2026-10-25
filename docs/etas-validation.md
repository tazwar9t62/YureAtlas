# ETAS numerical verification

This document defines reproducible checks for YureAtlas's discrete-space ETAS approximation. Passing them establishes software and numerical behavior; it does not establish earthquake forecasting skill, catalogue completeness or equivalence to a continuous-space implementation. Actual execution evidence is recorded in [verification.md](verification.md).

## Mathematical reference and declared approximation

[Ogata (1998), pp. 381, 384–385, equations 2.1, 2.5 and 2.6](https://www.ism.ac.jp/editsec/aism/pdf/050_2_0379.pdf) describes a history-dependent intensity, a response separated into productivity and normalized temporal/spatial factors, and a log likelihood equal to the sum of log intensities minus integrated intensity. The paper's finite-region integrals also make boundary treatment explicit.

YureAtlas implements the following discrete-cell version, with time in days:

```text
u[j] = area[j] / sum(area)
g(t) = (p - 1) / c * (1 + t/c)^(-p), t > 0
G(t) = 1 - (1 + max(t, 0)/c)^(1 - p)
k(m) = n * (beta - alpha) / beta * exp(alpha * (m - m0))
S[i,j] = area[j] * exp(-distance(i,j)^2 / (2*sigma^2))
         / sum_l(area[l] * exp(-distance(i,l)^2 / (2*sigma^2)))
lambda[j,t] = mu*u[j] + sum_(t_i<t) k(m_i)*g(t-t_i)*S[i,j]
integral = mu*(end-start) + sum_i k(m_i)*G(end-t_i)
negative_log_likelihood = integral - sum_i log(lambda[cell_i,t_i])
```

The integral assumes empty history at the fitting start and events inside the half-open fitting window. Spatial rows sum to one over retained cells; distances are haversine distances between cell centroids. Under exponential magnitude excess with rate `beta`, the unconditional offspring mean is `n`, requiring `alpha < beta` and `n < 1`. The fixed area background, cell discretization, finite-domain renormalization, no outside parents and fixed magnitude-independent spatial scale are deliberate approximations. Prediction uses at most 90 days of observed parent history and simulates the unobserved gap before the target as well as subsequent generations.

## Independent likelihood fixture

Use two cells with areas `[1,3]` and centroid separation `sigma*sqrt(2*log(2))`. The cross-cell Gaussian factor is exactly one half, giving spatial rows `[2/5,3/5]` and `[1/7,6/7]`; the background is `[1/4,3/4]`.

For fitting interval `[0,2)`, event times `[1/4,3/4,3/2]`, cells `[0,1,0]` and magnitudes `[4,5,6]`, choose `mu=2`, `n=2/5`, `alpha=log(2)`, `beta=2*log(2)`, `c=1`, `p=2`, and `m0=4`. The marked productivities are `[1/5,2/5,4/5]`. Direct rational arithmetic gives:

| Quantity | Independent expected value |
| --- | --- |
| First event intensity | `1/2` |
| Second event intensity | `3/2 + (1/5)*(4/9)*(3/5) = 233/150` |
| Third event intensity | `1/2 + (1/5)*(16/81)*(2/5) + (2/5)*(16/49)*(1/7) = 742447/1389150` |
| Integrated intensity | `4 + (1/5)*(7/11) + (2/5)*(5/9) + (4/5)*(1/3) = 457/99` |
| Negative log likelihood | `5.495401477878737` |

This asymmetric example detects parent/child transposition, missing magnitude productivity and an incorrect temporal integral. It does not depend on an optimizer claiming convergence.

The likelihood tests compare these values at relative tolerances of `1e-14` for intensities/spatial masses and absolute tolerances of `1e-13` for log likelihood. Independent time quadrature uses `1e-12` tolerances. They also check tied timestamps, input permutations, half-open windows, empty history, the optimizer's actual objective wiring, and a bounded 48-event real optimization whose objective is independently recomputed. An optimizer's convergence flag alone is insufficient evidence.

## Nonzero branching and simulation uncertainty

The simulation oracle uses one cell, no immigrants, `n=1/2`, `alpha=0`, `beta=2`, `c=0.02`, `p=2`, and one observed parent 0.02 days before the cutoff. The target starts at the cutoff and lasts one day. Its direct children have Poisson mean `lambda=25/104`. Any descendant in that interval requires a direct child there, so the exact hit probability is `1-exp(-25/104)`, approximately `0.21367463`. This is distinct from applying a Poisson transform to the mean of all generations.

An independent convolution calculation integrates exact temporal bin masses and bounds each generation's possible birth time. It uses 4,096 and 8,192 bins, 32 generations, and adds the remaining-generation bound `lambda*n^32/(1-n)`. Refining the bins narrows the mean interval to approximately `[0.46797634, 0.46798407]`. The construction uses neither the simulator's inverse CDF nor its event queue.

The fixed-seed check uses 10,000 simulations. Its hit tolerance is six binomial standard errors plus the maximum Jeffreys smoothing shift: about `0.024644`. For counts, a complete Poisson branching tree has second moment `1/(1-n)^3`; the resulting compound-Poisson variance bound gives a six-standard-error tolerance of about `0.083205`. The lower acceptable mean still exceeds the direct-child mean, so omitting later generations fails. These are declared numerical acceptance tolerances, not confidence claims about earthquake forecasts.

Separate scripted draws exercise a parent, a child born in the unknown gap, and a grandchild inside the target. Only the grandchild counts as an outcome. Other checks cover the exact 90-day lower parent boundary, exclusion of origins at or after the input cutoff, target magnitude filtering, seed repeatability, and Jeffreys smoothing. The production default remains 512 simulations with seed 2026: probability increments are `1/513`, the no-hit floor is about 0.0975%, and the maximum binomial standard error is about 2.21 percentage points.

## Failure and resource policy

Model parameters, arrays, cell indices, grid geometry and normalized background must be finite and compatible. Archived model state is validated again when used. Fitting requires 40–1,800 target events; the mathematical component helper also accepts smaller fixtures. Supported grids contain at most 10,000 cells. Simulation counts must be integers from 1 through 10,000, and the target may start between the cutoff and one day later; normal issuance still uses its fixed 30-minute gap.

The 20,000-event simulation cap includes observed parents, immigrants and generated descendants. Parent and immigrant limits are checked before creating their queues or random event arrays; child limits are checked before allocating child arrays. Overflow, invalid optimizer output, nonconvergence and resource failures produce unavailable/error results. The simulator never returns accumulated partial probabilities. These operating bounds are separate from the scientific approximation and Monte Carlo uncertainty.

## Reproduction

From the supported project environment, run the pure numerical cases with:

```sh
YUREATLAS_INTEGRATION=0 .venv/bin/python -m pytest \
  services/api/tests/test_etas_likelihood.py \
  services/api/tests/test_etas_simulation.py \
  services/api/tests/test_etas_validation.py -q
```

Use `make test-integration` for the saved-archive failure checks in an owned disposable database/artifact directory. `make test-unit` includes the numerical cases. See [the dated verification record](verification.md) for actual counts, timings, publication-path results and remaining limitations.

## External implementation assessment

The [CRAN ETAS 0.7.2 manual](https://cran.r-project.org/web/packages/ETAS/refman/ETAS.html) documents a maintained implementation descended from Zhuang's Fortran work, with a GPL (>=2) license. It fits a continuous-space model and estimates the background through iterative declustering. Its Gaussian option also permits magnitude-dependent spatial scale; region integrals and coordinate handling differ from YureAtlas's retained-cell masses.

The equivalent temporal density and productivity can be compared after the parameter mapping `A = n*(beta-alpha)/beta`. A matching fit, spatial likelihood or forecast comparison would additionally require aligned background estimation, boundary integration, coordinates, history and discretization. Comparing the current models' fitted parameters or raw likelihoods would conflate those choices. No third-party implementation code is vendored, and no external R-package fit is claimed as executed. The independent fixtures above test the declared approximation; external predictive validation remains pending.

For the likelihood definition, use Ogata's equation 2.6. The CRAN manual's displayed likelihood expression omits the logarithm on the event intensity; its surrounding description does not override the primary equation.
