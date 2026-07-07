# Task

Your goal is to fully prove the following declarations in `Syntax.Injectivity`,
which are currently axioms: `EqTp.inv_pi`, `EqTp.inv_sigma`, `EqTp.inv_Id`.
Fully formalize and prove anything necessary to correctly and fully formalize
and prove the axioms.

The source material, `logrel-coq` (in this run dir), features proofs of these
declarations in Coq (`prod_ty_inj` / `sig_ty_inj` / `id_ty_inj` in
`theories/TypingProperties/TypeInjectivityConsequences.v`). Your task is
essentially translation of these proofs appropriately.

## State inherited from prior runs (continue, do not redo earlier phases)

### Run 32 (latest) — `symLR_rec` authored, additive `adRTinv` field KERNEL-REFUTED, symmetry program shown ORTHOGONAL to keystone via build-verified dependency map; new sorry-free `LRtmEqWM.conv_dom`; pivot to category-D mutual recursion

**Master is at `d290325`** — UNCHANGED across Runs 30/31/32; `git log
d290325..HEAD` empty. Build GREEN. The 3 deliverable axioms remain
intact at `Injectivity.lean:14/19/25`. Two Run-32 PROVE iterations
landed committed progress on `worktree/chunk-1-2` (cannot land on
master — chain still carries the inherited `TermFundamental.lean`
sorries plus 2 sanctioned spine-goal sorries in `symLR_rec`).

**Forward-port progress committed across Runs 30–31** (on
`worktree/chunk-1-2`):

1. **Run 30 — `49c9acb`** (22 ahead, `backup/run30-progress-49c9acb`,
   $19.5). Authored `MergedLR.symLR_rec` — bespoke symmetry recursor
   over `LRtyEqM`. `univ`/`ne`/`Id` cases CLOSED sorry-free; `pi`/`sigma`
   each reduced to a single per-arg `LRtmEqWM` swap obligation (the
   `symRedTm` witness — `RT Δ ξ b a` from the abstract positivity
   vehicle). Build green-modulo-2-sanctioned-spine-sorries (131 jobs).
   The Run-30 critic flagged the 2 spine sorries as needing an
   additive inverse-adequacy field on the `MergedLR.pi/sigma`
   constructors — a path Run 31 then refuted.
2. **Run 31a — `82d8c5c`** (preserved). **Build-verified REFUTATION**
   of the Run-30 additive `adRTinv` field mandate. Adding
   `(∀ {Δ ξ a b}, Wk E Δ Γ ξ → RT Δ ξ a b → adRT Δ ξ b a)` to
   `MergedLR.pi/sigma` is kernel-impossible: it places the mutual
   sibling `LRtmEqWM` in negative position
   (`arg #22 of LRtyEqM.pi has a non positive occurrence`). The
   refutation **generalizes to ALL additive fields on the abstract-`RT`
   pack**: symmetry requires *constructing* an `RT` witness from a term
   witness, but abstract `RT` is write-only (accessible only via the
   producer), so any biconditional bridge `LRtmEqWM ↔ RT` forces
   `LRtmEqWM` into negative position. The single mutual-inductive block
   is *intrinsically* unable to host a symmetry-inverse field.
3. **Run 31b — `56be3ab`** (24 ahead of master,
   `backup/run31-progress-56be3ab` — **NEW frontier; seed Run 33 from
   this**, $17.6). Two breakthroughs:
   - **Build-verified dependency map** showing NONE of the 14
     `TermFundamental.lean` keystone sorries needs the merged-block
     `LRtyEqM.symm`. They are all blocked on mutual
     `WfTm`/`EqTm`/type-level Fundamental induction (the
     `Prop`-`SizeOf≡0` termination issue identified in Run 16) —
     **orthogonal** to the entire `symLR_rec` symmetry effort that 30
     prior runs chased.
   - New sorry-free, axiom-free reusable lemma `LRtmEqWM.conv_dom` in
     `MergedLR.lean` (line 272) — discharges domain-type conversion at
     the term-reducibility layer, useful regardless of which keystone
     route the next run picks.

**Verified by build + adversarial review (Run 32 critic,
NEEDS_REVISION case (a)):** the symmetry program (`symLR_rec`,
inverse-adequacy fields, `LRtyEqM → LRtyEq` bridge) is now
**provably DEAD** as the keystone path — kernel non-positivity on one
side, dependency-map orthogonality on the other. The real bottleneck
is the **mutual `WfTm`/`EqTm`/Fundamental induction's termination
measure** on `URedEq.fundamentalAux` (category D:
`conv_eq`/`refl_tm`/`symm_tm'`/`trans_tm'` arms), plus the
neutral-spine + redex head term-reducibility arms (categories A/B).

**Strategic redirect (highest-priority decision tagged `2a4c4942` in
forum):** stop chasing merged-block type symmetry. The next run must
attack either (i) category-D closure by verifying whether routing
through `MergedLR.posRed`'s reducible measure gives
`mutual_induction` / `termination_by` a decreasing argument, or (ii)
categories A/B via the mutual `WfTm` inductive.

### Superseded — Run 29 advance (still part of the cumulative frontier; symmetry framing now refuted) — `fundamental_valid` decoupled from standalone keystone for all 4 redex cases + `code_el` code branch; new reusable `URedEq.code_el_code` lemma; 4 successive sorry-free committed advances on top of `MergedLR`

**Master is at `d290325`** (Run 29 escalation iter-1 landed a cosmetic
fix: rewrote `test/basic.lean:89`'s deliberate `mltt def foo : Type :=
sorry` fixture to `:= B` so the strict zero-sorry policy holds without
losing the `#guard_msgs` test of the sorry-warning path — squash-merged
as `UNITY: merge chunk test/basic.lean`). Build GREEN (121 jobs). The 3
deliverable axioms remain intact at `Injectivity.lean:14/19/25`. **The
keystone has not landed on master.** Master is now sorry-free
end-to-end on the tracked `HoTTLean/Syntax/**` + `HoTTLean/ForPoly.lean`
surface (every `\bsorry\b` grep hit is a docstring/comment).

**Forward-port progress committed across Runs 27–29** (all on
`worktree/chunk-1-2`; none merged to master because the chain still
carries ~18 sorries in `TermFundamental.lean` inherited from Run 21):

1. **Run 27 — `c4c6a0b`** (18 ahead of master, `backup/run27-progress-c4c6a0b`).
   First *genuine* downstream consumer of `MergedLR`. New unconditional,
   `tyMap`-free forgetful lemmas `URedEqM.ne_forget` and
   `LRtmEqWM.forget_nonuniv` in `MergedLR.lean`. Rewired
   `URedEq.cong_ne_intro` in `UnivFundamentalThm.lean` to route through
   them. Build green (133 jobs); `#print axioms` clean on the ported
   theorems.
2. **Run 28 — `064924e`** (19 ahead of master, `backup/run28-progress-064924e`).
   Extended the neutral-leaf consumer set: `URedEq.bvar_intro` and the
   `ax_intro` leaf now also route through `URedEqM.ne_forget`. Build
   green (133 jobs); axiom-free on-branch.
3. **Run 29a — `318d648`** (20 ahead). Closed the **`idRec_refl'` redex
   case of `fundamental_valid` in-block**, removing one standalone-keystone
   dependency edge. The route is: instantiate `TmStep.idRec_refl` + use
   `Expr.subst_snoc_toSb_subst` to land the substituted-motive
   equation, then close via the in-block `mutual_induction` IH. No new
   files; no `sorry`. Backup `backup/run29-progress-318d648`.
4. **Run 29b — `5478333`** (21 ahead, `backup/run29b-progress-5478333`).
   Closed the **code branch of `code_el` in-block** via a new sorry-free
   reusable lemma `URedEq.code_el_code` in `Fundamental.lean`
   (universe-η `redSubst` at a code head). All 4 redex cases of
   `fundamental_valid` are now in-block; the η-mismatch *neutral* branch
   of `code_el` is the surviving wall — and that wall genuinely needs the
   merged-relation reorg, confirming the forward-only `MergedLR` consumer
   port is load-bearing, not optional.

**Verified by build + adversarial review (Run 29 critic, NEEDS_REVISION
case (a)):** the merged block `MergedLR` is the new logical relation;
the existing `LRtyEq`/`LRtmEqW`/`URedEq` cannot be recovered from it
without rewriting `RedTysW`. The 3 deliverable axioms remain blocked
behind the ~46-consumer-site, ~1500-LOC forward port. Each PROVE
iteration lands 1–4 sorry-free in-block decoupling commits; the
multi-run trajectory is concretely advancing, not stalled.

**Build-verified WALL-1 finding (Run 28):** symmetry/transitivity of the
merged relation via a single generic `mutual_induction LRtyEqM` does NOT
go through — the abstract `RT` pack is *positive-only* (no producer for
the swapped `RT b a`). A bespoke `symLR_rec` structural recursor over
`MergedLR` is required if/when the symmetry direction is needed. Do not
re-attempt the generic-mutual route for symm/trans.

### Superseded — Run 26 advance (still part of the cumulative frontier)

**Master is at `3052fb3`** (Run 25's escalation pass landed: 9 dead
`attic/*` + `test/unitt.lean` files removed and squash-merged to master as
`UNITY: merge chunk …` commits; `test/basic.lean:89` preserved
intentionally). Build GREEN (121 jobs). The 3 deliverable axioms remain
intact at `Injectivity.lean:14/19/25`. **The keystone has not landed on
master.**

**The Run 25 advance** (frontier `backup/run25-progress-1b147f6`, 15 ahead
of master): a new file `HoTTLean/Syntax/LogicalRelation/MergedLR.lean`
(~331 lines), kernel-accepted, sorry-free, axiom-clean. It is a **three-way
merged inductive block** `LRtyEqM ⨯ LRtmEqWM ⨯ URedEqM` defined
simultaneously with a strengthened `posRed : LRtmEqWM` field at the Π/Σ/Id
per-argument slots (the genuine reducible witness the Run-21 verdict
identified). The kernel-positivity wall that refuted the prior
"additive `posRed : LRtmEqW` field on existing `LRtyEq`" route (Run 25
finding) is dissolved here via an abstract-pack encoding (the `posRed`
carrier is parametric, instantiated to the merged-block field after the
type checks). `MergedLR` is a *parallel, unconsumed* relation alongside
the master `LRtyEq`/`LRtmEqW`/`URedEq` — landing it alone is
brick-accumulation, FORBIDDEN by the sorry-free invariant; it stays on
the worktree branch.

**The Run 26 advance** (frontier `backup/run26-progress-1e8f4d9`, **17
ahead of master**, +2 sorry-free commits on top of `1b147f6`):

1. **`57d1f13` — parameter-free merged escape via genuine `mutual_induction`.**
   `MergedLR`'s `escape_conj` + `escapeEqM`/`escapeEqTermM`/`escapeURedEqM`
   are now proved by a single simultaneous `mutual_induction LRtyEqM`,
   closing sorry-free across U/ne/pi/sigma/Id cases. This is the first
   structurally-recursive consumer of `MergedLR` to land — it
   demonstrates the merged block IS usable by the existing
   mutual-induction tactics, not merely kernel-acceptable. Sorry-free,
   axiom-clean.
2. **`1e8f4d9` — consumer port (item 2): forward-only forgetful maps
   closed sorry-free, backward bridge REFUTED.**
   - `URedEqM → URedEq` — **closes sorry-free** (11/13 cases discharge
     mechanically by `mutual_induction` + constructor chaining).
   - `LRtmEqWM → LRtmEqW` — **closes sorry-free** (same pattern).
   - `LRtyEqM → LRtyEq` — **build-verified REFUTED.** The abstract `RT`
     pack carrier has no introduction form into the existing `LRtyEq`'s
     concrete `RedTysW`-based pi/sigma fields; the kernel rejects every
     attempt to construct an `LRtyEq.pi`/`sigma`/`Id` from an `LRtyEqM`
     witness because the `posRed`-strengthening means the field types
     are *genuinely* incomparable. There is no backward bridge.

**Build-verified architectural verdict (Run 26, sharpest to date):** the
keystone route is **forward-only**. The merged block `MergedLR` is the
*new* logical relation; the existing `LRtyEq`/`LRtmEqW`/`URedEq` cannot
be recovered from it without a ground-up rewrite of `RedTysW`. The 3
deliverable axioms must therefore be discharged by **porting the ~46
consumer sites** (everything in
`UnivFundamental.lean`/`UnivFundamentalThm.lean`/`Fundamental.lean`/
`TermFundamental.lean`/`Validity.lean`/`RedSubst.lean`/the `Consequences`
chain) onto the enriched `MergedLR` relation. This is a ~1500-LOC
mechanical-but-vast reorganization; it is beyond any single
PROVE-iteration budget but is *not* architecturally blocked. **The
prior "inline into `mutual_induction WfCtx`" plan is now superseded** —
`MergedLR` IS the inlined block, factored out of `Fundamental.lean` only
for separation-of-concerns. The `mutual_induction` motive change is no
longer the mandatory first action.

### Frontier branches (Run 25 → Run 31)

- **`backup/run31-progress-56be3ab`** (24 ahead of master; **CURRENT
  keystone frontier — seed Run 33 from THIS**). Carries the full
  Run 26 → Run 31 chain: `MergedLR.lean` + escape layer + forward
  forgetful maps + 4 in-block decoupling commits + `URedEq.code_el_code`
  + `symLR_rec` skeleton (univ/ne/Id sorry-free, pi/sigma fanned to 2
  spine sorries) + new sorry-free `LRtmEqWM.conv_dom` (line 272) + the
  build-verified dependency map proving symmetry orthogonal to the 14
  keystone sorries. Worktree carries 14 `TermFundamental.lean` residual
  sorries + 2 sanctioned spine sorries in `MergedLR.symLR_rec`.
- **`backup/run30-progress-49c9acb`** (22 ahead; preserved). Run 30 —
  `symLR_rec` initial authoring (univ/ne sorry-free; pi/sigma fanned).
  Reference for the recursor design only; superseded by `56be3ab`.
- **`backup/run29b-progress-5478333`** (21 ahead of master; preserved).
  Carries Run 26 → Run 29 chain: `MergedLR.lean` + escape layer +
  forward forgetful maps + 4 in-block decoupling commits + new
  sorry-free reusable `URedEq.code_el_code` lemma. Reference; superseded
  by Run-30/31 frontier.
- **`backup/run29-progress-318d648`** (20 ahead; preserved). Run 29a —
  `idRec_refl'` redex case closed in-block.
- **`backup/run28-progress-064924e`** (19 ahead; preserved). Run 28 —
  `bvar_intro`/`ax_intro` consumer port via `ne_forget`.
- **`backup/run27-progress-c4c6a0b`** (18 ahead; preserved). Run 27 —
  first genuine `MergedLR` consumer, `cong_ne_intro` via `ne_forget`.
- **`backup/run26-progress-1e8f4d9`** (17 ahead; preserved). Run 26
  baseline: `MergedLR.lean` + parameter-free `mutual_induction`-based
  escape layer + sorry-free forward forgetful maps.
- **`backup/run25-progress-1b147f6`** (15 ahead of master; preserved).
  First `MergedLR.lean` kernel-accepted draft; superseded by `1e8f4d9`.
- **`backup/run25-progress-aab0bf5`** (preserved). Earlier Run 25
  frontier.
- **`backup/run23-progress-240e101`** (10 ahead; preserved). Run 23/24
  partial — pre-`MergedLR`. Reference only.
- **`backup/run21-progress-f945b1a`** (7 ahead; preserved). Run 21
  whred-expansion helpers. Reference only.

### Run 21/22 — keystone narrowed via whred-expansion helpers (historical, pre-`MergedLR`)

**Master is at `1ac026b`** (33 commits ahead of `origin/master` before the
Run-12 escalation pass; the escalation pass landed an in-progress series of
`UNITY: merge chunk attic/…` commits removing dead unbuildable `attic/*`
files — the loop was killed mid-pass on NFS lock contention with ~3–4 of 20
deletions landed (`attic/Display.lean`,
`attic/FibrationForMathlib/Displayed/Fibration.lean`,
`attic/FibrationForMathlib/FibredCats/CartLift.lean`); the remaining ~16
worktree branches carry their `ESCALATION: remove dead unbuildable chunk …`
commit but were not squash-merged. Master HEAD therefore is some
`514daed`-or-later cleanup commit, NOT a keystone advance). The 3
deliverable axioms have not moved on master in 5 runs.

The Run-20 advance that brought master to `1ac026b` (`80b1877 → d456a1e →
67f1ed3 → 1ac026b`) was the **reducible Kripke per-arg families brick**
`TermLRApp.lean`: `LRtmEqW.redConv` + `of_pi/sigma/Id/univ_red` smart
constructors + `LRPiTmEqRed` (PolyRed-style classifier-parametric reducible
Π Kripke application family). Sorry-free, axiom-clean (only
`propext`/`Classical.choice`/`Quot.sound`), additive. Run 19's `TermLRKripke`
brick is also on master. **The keystone (`URedEq.fundamental` /
`redValidTy`) remains unresolved on master.**

**The genuinely new Run 21 finding (frontier
`backup/run21-progress-f945b1a`, 7 ahead of master, 2 new commits):**
sorry-free **whred-expansion discharge helpers** for the 4 category-(B)
β/σ/ι redex cases of `URedEq.fundamentalAux`. The helpers
`URedEq.{app_lam,fst_pair,snd_pair,idRec_refl}_whred` take an *explicit*
`(hred : URedEq E Γ l₀ reduct reduct)` reduct-canonicity hypothesis plus the
already-available judgmental data (`hA`, `hB`, `ht`, `hu`, …) and produce
the `URedEq` at the redex via the `whred_l`/`whred_r` constructors. Each of
the 4 category-(B) `URedEq.fundamentalAux` cases is then wired through its
helper, narrowing the residual `sorry` to exactly **`URedEq E Γ l reduct
reduct`** (reflexive reduct canonicity at the reduct). The helpers are
axiom-clean (only `propext`/`Classical.choice`/`Quot.sound`); both
`Fundamental` and `TermFundamental` build green-modulo-sorry (134 jobs).
**14 narrow real sorries remain** in `TermFundamental.lean` (16→14 across
Run 21).

**Build-verified architectural verdict (Run 21, sharpened):** in-place
narrowing of standalone `URedEq.fundamentalAux` is dead. The sole
gap-reducing route is the simultaneous (a) **posRed-carries-`LRtmEqW`**
strengthening (so spine-IH cases yield genuine reducible witnesses, not the
vacuous `TmValidU` at non-universe slots) + (b) Kripke reducible families
for Σ/Id (mirroring Run 20's `LRPiTmEqRed` — additive, additional bricks
permitted ONLY when they discharge an *identified* spine case) + (c)
**mutual inlining** of the term-universe canonicity recursion as a sibling
motive in `Fundamental.lean`'s `mutual_induction WfCtx`. The standalone
`Prop`-`SizeOf` and import-cycle walls (Run 16) remain in force.

**The genuinely new Run 16 finding — and why progress stalled:** the prover
attempting `URedEq.fundamental` as a **standalone `Prop`-valued theorem** in
its own module (`TermFundamental.lean`) hits TWO independent walls that
together kill the architecture:

1. **Import cycle.** `TermFundamental.lean` would have to sit **upstream** of
   `Fundamental.lean` (so the type-side `fundamental_valid` can consume it
   as a corollary). But the keystone's term-side cases (`app`, `fst`, `snd`,
   `idRec`, `code`, `el`, `ax`, `bvar` at universe slot) all need the
   *type-side* `redValidTy`/`fundamental_valid` as IHs — which the standalone
   ordering forbids.
2. **`Prop`-`SizeOf` degeneracy.** Lean auto-generates `SizeOf P ≡ 0` for any
   `P : Prop` (judgments `WfTm`, `EqTm` are `Prop`-valued). `cases h` then
   yields zero structural IH. Standalone well-founded recursion on
   `sizeOf h : EqTm …` is therefore **impossible** — every subgoal's
   decreasing condition becomes `0 < 0`. The Run 16 prover diagnosed this
   precisely by running `decreasing_by trace_state` and observing the goal.

**Combined corollary:** factoring `URedEq.fundamental` as a *separate*
recursion is architecturally dead. The route forward is **inlining the
keystone INTO the existing `mutual_induction WfCtx`** block on
`backup/run16-progress-3234c92`'s `Fundamental.lean`, where (a) the
`mutual_induction` macro generates structural IHs without going through `SizeOf`,
and (b) type-side and term-side cases are simultaneously available.

**Verified at master `1ac026b` (pre-escalation-pass):**
- `lake build HoTTLean.Syntax.Injectivity` → 121/121 jobs, **GREEN**.
- Exactly the 3 deliverable axioms remain (`Injectivity.lean:14/19/25`).
  Only other axioms are `Prelude.lean` `sorryAx₀/₁/₂` (pre-existing upstream,
  commit `8a2ecd9`, PR #133, Sep 2025).
- No project-introduced `sorry`/`admit` on the tracked `HoTTLean/Syntax/**` +
  `HoTTLean/ForPoly.lean` surface.
- **No circularity**: `EqTp.inv_*` referenced nowhere in the LR development.
- `TermLRKripke.lean` (Run 19) and `TermLRApp.lean` (Run 20) both present,
  axiom-clean, building green.

### Frontier branches (Run 19 → Run 21)

- **`backup/run21-progress-f945b1a`** (7 ahead of master; latest keystone
  frontier). The 4 whred-expansion discharge helpers landed sorry-free; each
  category-(B) `URedEq.fundamentalAux` arm wired through its helper;
  residuals collapsed to `URedEq r r`. **THIS is the seed for the next
  prover** — combined with master's `TermLRApp`/`TermLRKripke` Kripke bricks.
- **`backup/run20-progress-b2d7cc3`** (preserved). Source of the `TermLRApp`
  reducible Kripke Π application family before it landed on master.
- **`backup/run19-progress-46ede3d`** (preserved). Source of the
  `TermLRKripke` judgmental Kripke families before it landed on master.

### Historical frontier branches (Run 15 + Run 16) — reference only

- **`backup/run15-progress-2ce1245`** (3 ahead of master). Run 15 prover
  collapsed the 6 funnel-sorries in `Fundamental.lean` into **ONE** named
  keystone `sorry` at `TermFundamental.lean:53`. `Fundamental.lean` itself
  becomes **fully sorry-free** on this branch. This is the cleanest "shape of
  the problem" frontier — useful as a reference, not as a seed for the next
  prover (the standalone framing is now refuted).
- **`backup/run16-progress-3234c92`** (3 ahead of master; latest frontier).
  Run 16 attempted to discharge the standalone keystone by `cases h` on the
  `EqTm`-at-`univ` derivation. Produced a **structured proof skeleton with
  ~16 narrow residual sorries** in `TermFundamental.lean` plus the architectural
  diagnosis above (import cycle + `Prop`-`SizeOf` degeneracy). 5 structural-absurd
  case families closed sorry-free as a byproduct. **This branch is the seed for
  Run 18**, but ONLY after the keystone is moved into the mutual block — do
  not attempt to land it as-is.
- **`backup/run14-progress-22b1755`** (the original 21/27 case skeleton)
  remains the canonical reference for the `mutual_induction WfCtx` block
  structure on `Fundamental.lean`. Run 16's branch already incorporates it.

**Master timeline of sorry-free additive landings (Run 9 → 13):**
- Run 9 (`03a7810 → af77bb5`): `LRtyEq`/`RedTysW` relational engine,
  `UnivReducible`/`RedStable`/`ElBridge`/`ElCodeBranch`/`UnivFundamental`/
  `UnivFundamentalThm`, `Consequences.lean` `EqTp.conv_inj_red` (conditional
  `ty_conv_inj`), `ForPoly.lean` `snd'_verticalNatTrans_app`.
- Run 13 (`af77bb5 → c91ce7f → 784c5f8`):
  - **`RedSubst.lean`** — `VRU`/`eqSbU` refined validity environment carrying
    `URedEq` head-witnesses at universe slots; `VRU.bvar_motive` leaf closed
    sorry-free.
  - **`TermLR.lean`** — `LRtmEqW` term-level reducible equality (5 cases) +
    `escape`/`symm`/`rename`/`of_univ`/`to_univ` bridges; whnf-head-parallel
    structure has *no* termination/positivity obligation (correcting the prior
    sizeOf-W assumption).

### The sole remaining obstruction — TWO NAMED PROP ORACLES (unchanged on master)

```lean
-- HoTTLean/Syntax/LogicalRelation/UnivFundamentalThm.lean:162-168
abbrev ReflTyOracle (E : Axioms) : Prop := ∀ Γ l A, (E ∣ Γ ⊢ A) → LRtyEq E Γ l A A
abbrev CongTyOracle (E : Axioms) : Prop :=
  ∀ Γ l A A', (E ∣ Γ ⊢ A ≡ A') → LRtyEq E Γ l A A'
```

These are honest `Prop` hypotheses, **not** project axioms, consumed as
explicit parameters everywhere they appear. **`CongTyOracle E` IS the
`redValidTy` keystone.**

### Run 14 frontier: simultaneous induction ASSEMBLED with 21/27 cases sorry-free

Run 14 built — for the first time across the entire project history — a
working `Fundamental.lean` on `worktree/chunk-1-2` /
`backup/run14-progress-22b1755` that:
- Declares `fundamental_valid` as ONE `mutual_induction WfCtx` over
  `WfTp`/`EqTp`/`WfTm`/`EqTm` with motives `(eqTl, hVΓ) ↦ ValidTyEqU` /
  `(eqTl, hVΓ) ↦ TmValidU` (the `VRU`-native pair from Run 13's `RedSubst`).
- Closes **21 of 27 constructor cases sorry-free + axiom-clean**: `univ`,
  `Id'`, `cong_Id`, `refl_tp`, `trans_tp`, `el`, `cong_el`, `el_code`,
  `pi'`/`sigma'`/`cong_pi'`/`cong_sigma'`, `code`, `cong_code`, `refl_tm`,
  `trans_tm`, `bvar`, plus 8 vacuous-at-univ-slot term cases
  (`lam'`/`cong_refl'`/`pair_fst_snd'`/etc.).
- The bvar leaf was closed via a **level-decoupling fix in `RedSubst`**:
  `VRU.slot_ured`/`bvar_motive` now take the lookup-slot level and the
  universe-contents level as *independent* parameters — the prior coupling
  was load-bearing in name only.
- 3 sorry-free corollaries `reflTy_of_fundamental` / `congTy_of_fundamental`
  / `VRU.of_wfCtx` (via list-recursion).
- Branch builds green with only sorry warnings (135 jobs).

**Residual: 6 sorries** on `worktree/chunk-1-2` HEAD `22b1755` Fundamental.lean
at lines 305/355/363/371/379/460 (`symm_tp`, `code` consequence, and four
other points). The prover diagnosed all 6 as funneling to a **single named
blocker**: `URedEq.fundamental` (term-universe canonicity / `EqTm`-at-univ ⇒
`URedEq`). This is the ~1200-LOC term-level logical-relation theorem that has
resisted all 13 prior runs; closing it needs the full LRtmEqW Fundamental
machinery built on top of Run 13's `TermLR.lean`, not a localized fix.

**This branch is NOT merged to master.** Master's sorry-free invariant
explicitly forbids landing a sorried chain.

### Branches now demoted to historical (do not seed from)

- **`run8-landed`** (`df1d70a`), **`backup/run9-progress-1e9f1ca`**,
  **`backup/run11-progress-6df21c4`**, **`backup/run12-progress-1e9f1ca`** —
  prior frontier attempts (each 13–22 sorries). All **OBSOLETE.**
- **`backup/run7-landed-progress`** and older
  (`keystone-mutual-block`, `reconciled-13`, `iter-prove-base`,
  `residual-narrow`, `partial-chain-da05878`, `worktree/frontier-posred-univ`)
  — historical only.

## HARD CONSTRAINTS FOR RUN 33 (read FIRST, before any other section)

**The Run-30 HARD CONSTRAINTS targeted the wrong bottleneck.** Run 31
proved this with two build-verified results: (1) the additive
`adRTinv` inverse-adequacy field that would have closed `symLR_rec`'s
spine sorries is **kernel-impossible** (non-positive occurrence in the
mutual block), and the refutation generalizes to ALL additive fields
on the abstract-`RT` pack; (2) a build-verified dependency map shows
the merged-block symmetry program is **ORTHOGONAL** to the 14
keystone `TermFundamental.lean` sorries — none of them needs
`LRtyEqM.symm` to close. The surviving 30-run "symmetry is the
bottleneck" framing is REFUTED. The real bottleneck is the
**mutual-recursion termination measure on `URedEq.fundamentalAux`**
(category D: `conv_eq`/`refl_tm`/`symm_tm'`/`trans_tm'`) plus the
neutral-spine + redex head-term-reducibility arms (categories A/B).

### Mandatory first action

The formalization prover's **FIRST committed edit** must be **EXACTLY
ONE** of:

**(a) Category-D probe — `mutual_induction` measure via `MergedLR.posRed`.**
At one of the 4 category-D arms of `URedEq.fundamentalAux` (the
`conv_eq`/`refl_tm`/`symm_tm'`/`trans_tm'` cases in
`TermFundamental.lean` — pick by `file:line` in the commit message),
verify whether routing the recursive call through `MergedLR.posRed`'s
reducible witness gives Lean's `mutual_induction` / `termination_by`
a decreasing measure that the `Prop`-`SizeOf≡0` degeneracy doesn't
kill. Commit either: (i) a sorry-free closure of the arm via the
posRed-measured route, or (ii) a build-verified diagnosis explaining
which specific recursive call the `Prop`-`SizeOf` collapse blocks, on
which constructor's IH, with the failing `decreasing_by` goal printed
verbatim. A diagnosis-only commit is acceptable only if it is as
sharp as Run 16's standalone-keystone refutation.

**(b) Category-A/B head-term reducibility port.** Pick ONE of the
neutral-spine (`cong_app'`/`cong_fst'`/`cong_snd'`/`cong_idRec'`/
`cong_code`) or redex (`code_el` neutral branch) arms of
`URedEq.fundamentalAux`. Discharge it sorry-free by routing the head
term-reducibility obligation through the merged block's
`LRtmEqWM`/`URedEqM` forward forgetful maps + the new
`LRtmEqWM.conv_dom` lemma (`MergedLR.lean:272`, available on
`backup/run31-progress-56be3ab`). Single sorry-free closure on a
single named arm; no skeleton, no partial.

**(c) Discharge ONE specific `TermFundamental.lean` sorry by ANY
route.** Pick a single line by `file:line`, state the goal in the
commit message, and close it sorry-free. Highest-confidence fallback
if (a) and (b) prove too speculative within budget.

**No other edits are permitted until that commit exists.**
Specifically forbidden as a Run 33 first commit:

- Any continuation of the `symLR_rec` / symmetry program — provably
  orthogonal to the keystone per Run-31 decision `2a4c4942`.
- Any additive field on `MergedLR.pi/sigma/Id` constructors — kernel
  non-positivity per Run-31 decision `721532a7`.
- Another `URedEq.*_intro` LEAF consumer-port extension. The neutral
  leaves done in Runs 27/28 do not narrow the category-D bottleneck.
- A new abstract helper, namespace reorg, or "preparatory" refactor.
- Rewriting `MergedLR` itself, its `posRed` field, its `symLR_rec`
  skeleton, or its forgetful maps.
- Re-attempting the refuted backward bridge `LRtyEqM → LRtyEq` (Run 26
  REFUTED) or generic-mutual symmetry `mutual_induction LRtyEqM` for
  symm/trans (Run 28 REFUTED) or the `adRTinv` inverse-adequacy field
  (Run 31 REFUTED).

If the prover's first commit is anything else, the orchestrator MUST
reject the worktree and re-dispatch with the constraint quoted
verbatim.

### Forbidden moves

- **DO NOT re-attempt the `LRtyEqM → LRtyEq` forgetful bridge.** Run 26
  build-verified this as REFUTED on kernel-positivity grounds. Two prior
  runs (24, 25) burned ~$100 cumulatively rediscovering walls of this
  shape. Skip it.
- **DO NOT re-attempt symmetry/transitivity of `MergedLR` via a single
  generic `mutual_induction LRtyEqM`.** Run 28 build-verified this as
  WALL-1-blocked: the abstract `RT` pack is positive-only and has no
  producer for the swapped `RT b a`.
- **DO NOT add any additive inverse-adequacy / biconditional field to
  `MergedLR.pi/sigma/Id`.** Run 31 build-verified this as kernel
  non-positivity (`arg #22 of LRtyEqM.pi has a non positive
  occurrence`). The refutation generalizes to ALL additive fields
  bridging `LRtmEqWM ↔ RT`. Live route is NOT to extend the mutual
  block.
- **DO NOT continue the `symLR_rec` / merged-block symmetry program as
  if it were keystone-blocking.** Run 31 build-verified it
  ORTHOGONAL to the 14 `TermFundamental.lean` keystone sorries via a
  dependency map. The symmetry recursor on `backup/run30-progress-49c9acb`
  may stay as a reference but **do NOT chase its 2 spine sorries** as
  if discharging them would close the keystone — it would not.
- **DO NOT re-derive `MergedLR.lean` from scratch.** It is sorry-free,
  kernel-accepted, and committed at `backup/run26-progress-1e8f4d9`.
  Seed and consume it; do not rewrite it.
- **DO NOT land `MergedLR.lean` or any of its consumers on master** until
  at least one consumer-site port is sorry-free AND a corollary
  reduction in `TermFundamental.lean` sorries is demonstrated. The
  sorry-free invariant on master forbids `MergedLR` alone (it is
  unconsumed brick-accumulation as Run 25's decision noted).
- **DO NOT create more new `.lean` files.** All work is editing
  `MergedLR.lean`'s consumer chain or `TermFundamental.lean`. Helper
  lemmas go inside an *existing* file.
- **DO NOT spend more than 5 cumulative minutes on `lake build` polling
  per subagent.** Run 20 burned ~$30 of $97 on polls. Background long
  builds; check the log AT MOST twice per subagent.
- **DO NOT delete attic files.** Run 25's escalation pass finished the
  sanctioned cleanup; Run 29's escalation iter-1 finished the
  `test/basic.lean:89` cosmetic fix. Skip the escalation phase or
  NO-OP it immediately — there is nothing left to escalate outside the
  keystone.

### Mandatory seeding

```bash
cd /data/user_data/trowney/hott/HoTTLean
git -C .worktrees/chunk-1-2 reset --hard backup/run31-progress-56be3ab
```

`backup/run31-progress-56be3ab` (24 ahead of master) is strictly ahead
of every prior backup ref and carries the full Run 26 → Run 31 chain:
`MergedLR.lean` + escape layer + forward forgetful maps + 4 in-block
decoupling commits + `URedEq.code_el_code` + `symLR_rec` skeleton + new
sorry-free `LRtmEqWM.conv_dom` (line 272) + the build-verified
dependency map proving symmetry orthogonal to the keystone. **Do NOT
seed from master** (master lacks `MergedLR` entirely). Do NOT seed from
`run26`/`run27`/`run28`/`run29`/`run30` — they are strictly behind
`56be3ab`. The `LRtmEqWM.conv_dom` lemma and the dependency-map proof
are the key Run-31 advances the next run must build on.

### Success criteria (in priority order)

1. **Full success:** `Injectivity.lean`'s 3 axioms promoted to theorems
   AND `lake build HoTTLean.Syntax.Injectivity` green AND
   `#print axioms EqTp.inv_pi` shows only
   `propext`/`Classical.choice`/`Quot.sound`.
2. **Substantial credit:** ≥3 category-D arms of
   `URedEq.fundamentalAux` closed sorry-free via the
   `MergedLR.posRed`-measured route, OR ≥3 category-A/B arms closed
   via the forward forgetful maps + `LRtmEqWM.conv_dom`; build
   green-modulo-the-remaining-sorries; per-arm status documented.
3. **Minimal credit:** 1 category-D arm closed sorry-free OR 1
   category-A/B arm closed sorry-free OR 1 `TermFundamental.lean`
   sorry discharged by any route OR a build-verified diagnosis as
   sharp as Run-16's standalone-keystone refutation showing exactly
   which `Prop`-`SizeOf` collapse blocks the posRed-measured route.
4. **No credit:** continuation of the symmetry program; another
   additive field on `MergedLR.pi/sigma`; another leaf consumer-port
   extension; a new parallel relation/abstract pack; any re-attempt of
   a refuted route (backward bridge, generic-mutual symm, adRTinv).

### Budget ceiling

Stop the run if cumulative spend exceeds **$150** without reaching
minimal credit (step 3 above). Cumulative project spend is now ≈ **$668**
across 32 runs (unity-15.out superrun ≈ $103, of which Run 30 PROVE
**$19.5**, Run 31 PROVE **$17.6** producing the build-verified
dual-refutation of the symmetry program, Run 32 critic + retro
**$29.9** with no PROVE due to 3-iter cap). Further autonomous
iteration past **$850 total** without master-side progress should be
considered evidence the keystone needs human-driven engineering. The
ship-or-iterate decision is now well-formed: the artifact carries
build-verified architectural diagnoses across 32 runs (kernel-positivity
wall for symmetry; dependency-map orthogonality of symmetry to
keystone; live route pinned to mutual-recursion termination measure),
and that may be the strongest output regardless of whether the 3
axioms close.

## Suggested approach on rerun (Run 33)

1. **`git checkout master`** at `d290325` (UNCHANGED since Run 29
   escalation iter-1). Confirm `lake build HoTTLean.Syntax.Injectivity`
   green; only project axioms are the 3 deliverables.
2. **Seed FORMALIZATION from `backup/run31-progress-56be3ab`** —
   carries the full Run 26–31 chain: `MergedLR.lean` + escape layer +
   forward forgetful maps + 4 in-block decoupling commits +
   `URedEq.code_el_code` + `symLR_rec` skeleton + new sorry-free
   `LRtmEqWM.conv_dom` (line 272) + the build-verified dependency map.
   Inherits 14 residual sorries in `TermFundamental.lean` + 2
   sanctioned spine sorries in `MergedLR.symLR_rec`.
3. **Do NOT re-attempt the `LRtyEqM → LRtyEq` forgetful bridge.** Run
   26 build-verified it REFUTED on kernel-positivity grounds; the
   route is forward-only.
4. **Execute the forward-only consumer port.** Pick the lowest-numbered
   file in the consumer chain that depends on `LRtyEq`/`LRtmEqW`/
   `URedEq` (candidates in dependency order: `RedSubst.lean`,
   `Validity.lean`, `UnivFundamental.lean`, `UnivFundamentalThm.lean`,
   `Fundamental.lean`, `TermFundamental.lean`). For each file:
   - Rewrite declarations to consume the `*M` variant from `MergedLR`.
   - Use Run 26's forward forgetful maps to discharge obligations
     stated in the old types.
   - Keep each file sorry-free at commit time.
5. **For each spine-case sorry in `TermFundamental.lean`:** check
   whether `MergedLR.posRed` (the strengthened reducible witness at
   the per-arg slot) directly discharges it. If yes, commit that
   sorry-closure as a standalone advance even before the full
   consumer port lands.
6. **Land additive sorry-free bricks to master incrementally** (Run
   9/13/14/19/20 discipline). `MergedLR.lean` MAY land on master only
   as part of a commit that ALSO ports at least one consumer site;
   `MergedLR` alone is brick-accumulation (Run 25 decision).
7. **Wire `redValidTy` → `conv_inj_red` → `ty_conv_inj` → the 3
   injectivity theorems** once the consumer chain reaches
   `Consequences.lean`. Each is a one-line corollary once
   `ReflTyOracle`/`CongTyOracle` are discharged. Promote
   `Injectivity.lean`'s 3 `axiom`s to `theorem`s **only after** `lake
   build` is sorry-free and `#print axioms` shows no
   project-introduced axioms.
8. Background long builds; emit heartbeats; never let a build run
   silently for >5 min. NFS commit/merge operations take 1–2 minutes
   each — serialize git ops; do not fan out parallel worktree commits.

## Constraints

- Do NOT redo source-scan, generation, validation, semiformalization, or
  exploration — their artifacts are on disk and re-verified through Run 17.
  These phases all NO-OP cleanly in continuation runs.
- **DO NOT attempt a standalone `URedEq.fundamental` theorem in its
  own module.** Run 16 proved this architecturally impossible (import
  cycle + `Prop`-`SizeOf` degeneracy). `MergedLR.lean` IS the merged
  inductive that replaces the standalone framing — consume it
  forward, do not factor it back out.
- **DO NOT re-attempt the `LRtyEqM → LRtyEq` forgetful bridge.** Run
  26 build-verified this REFUTED. The route is forward-only: port
  consumers onto `MergedLR`.
- All work is additive on top of master (`d290325`, carrying Run-19
  `TermLRKripke`, Run-20 `TermLRApp`, Run-25 attic cleanup, Run-29
  `test/basic.lean` cosmetic fix); do not regress any sorry-free file.
  Master must STAY sorry-free; never merge a branch that introduces a
  sorry or that promotes the 3 axioms to `theorem` while resting
  transitively on sorries. **For Run 33 specifically:** see the HARD
  CONSTRAINTS section at top — seed from
  `backup/run31-progress-56be3ab`; target category-D mutual-recursion
  termination measure or category-A/B head-term reducibility, NOT
  symmetry; `MergedLR.lean` lands on master only bundled with a
  reduction in `TermFundamental.lean`'s sorry count.
- **DO NOT revert** to: (a) the abstract-pack-only approach (Runs 1–7
  exhausted it); (b) the single-argument-posRed redesign (Run 7 refuted from
  Coq source); (c) the custom-recursor `indLR`/`symLR_rec` as the symm/trans
  path (Run 8 made this unnecessary); (d) parking sorries on an unmerged
  `run11-landed` frontier branch (Runs 7–8 spent 2 cycles in that mode
  without master ever advancing — Run 9/13's commit-to-master discipline is
  what finally unblocked progress).
- **DO NOT introduce any new `Prop`/`abbrev` oracle, hypothesis, or
  "conditional" wrapper to defer the keystone.** Master has exactly TWO
  named oracles (`ReflTyOracle E`, `CongTyOracle E`). The job is to
  DISCHARGE them — close `CongTyOracle E` via the simultaneous
  `WfTp`/`WfTm`/`EqTp`/`EqTm` induction (assembled on
  `backup/run14-progress-22b1755`, extended on
  `backup/run16-progress-3234c92`) **with the universe-canonicity recursion
  inlined as a sibling motive**, NOT as a downstream theorem. Run 15
  attempted a third oracle (`SpineCanonOracle`) and was REJECTED. Factoring
  out more named oracles is REJECTED.
- **DO NOT re-instantiate the Run 8 genuine-pack `LRtyAtW`
  `def`-on-`sizeOf-W` apparatus** on master. The relational `RedTysW` +
  named-Prop-oracles route is now merged. The genuine-pack files
  (`LRWf.lean`, `LRWfConvCtx.lean`, `LRWfClosed.lean`) exist ONLY on
  obsolete branches.
- When dispatching prover children that mutate the project: give each its own
  worktree (never the main checkout). **Always create a durable
  `backup/run18-progress-<sha>` ref before any worktree destruction or branch
  deletion** (Runs 8/9 both lost branches; both recovered via `git fsck
  --lost-found`).
- No `sorry`, no project-level `axiom` other than the 3 being promoted.
- **API budget.** Per-PROVE costs across recent runs: Run 14 = $56,
  Run 15 = $55, Run 16 = $29 (diagnostic), Run 19 = ~$30 (additive
  `TermLRKripke` brick), Run 20 = **$97** (largest single PROVE on
  record — extensive build polling during the
  `TermLRApp`/`LRPiTmEqRed` brick development) + $89 background, Run
  21 = $56 (whred helpers + pinned-route diagnosis), Run 23 ≈ $70
  PROVE (committed 4b5a16d/240e101; route sharpening), Run 25 ≈ $50
  PROVE (committed `MergedLR.lean` sorry-free kernel-accepted),
  Run 26 = $48 PROVE (committed 57d1f13 + 1e8f4d9: merged escape
  via mutual_induction + sorry-free consumer port + REFUTED-backward
  finding), **Run 27 = $14.5 PROVE** (committed c4c6a0b: first
  genuine MergedLR consumer via `ne_forget`), **Run 28 = $15.0
  PROVE** (committed 064924e: `bvar_intro`/`ax_intro` consumer port +
  WALL-1 symm finding), **Run 29 = $32.65 PROVE** (committed 318d648
  + 5478333: `idRec_refl'` redex in-block via
  `TmStep.idRec_refl`/`Expr.subst_snoc_toSb_subst` + `code_el` code
  branch in-block via new sorry-free `URedEq.code_el_code`),
  **Run 30 = $19.5 PROVE** (committed 49c9acb: `MergedLR.symLR_rec`
  skeleton — univ/ne/Id sorry-free, pi/sigma fanned to single per-arg
  `LRtmEqWM` swap obligations), **Run 31 = $17.6 PROVE** (two
  build-verified architectural results: (a) `82d8c5c` refuting the
  additive `adRTinv` field on kernel non-positivity grounds; (b)
  `56be3ab` build-verified dependency map proving `symLR_rec`
  ORTHOGONAL to the 14 keystone sorries + new sorry-free
  `LRtmEqWM.conv_dom`), **Run 32 ≈ $29.9** (no PROVE — 3-iter cap hit
  on critic loop; master `d290325` unchanged).
  unity-14.out superrun ≈ $110 across 3 iterations.
  unity-15.out superrun ≈ $103 across 3 iterations.
  **Total burn for the keystone work since Run 14 ≈ $668.**
  Plan for fresh quota; the live route (mutual-recursion termination
  measure on `URedEq.fundamentalAux` + category-A/B head-term
  reducibility) is ~5–10× larger than any single PROVE can cover and
  may cost an additional $200–500 spread across multiple runs — but
  with the symmetry program now build-verified DEAD, future runs are
  no longer at risk of burning budget on the wrong direction.

## Operational notes (do not repeat past failures)

- **API billing cap.** Runs 9, 10, and 11 all crashed mid-run on quota
  exhaustion (Run 9 at iter-3 exploration ~1:10am, Run 10 at iter-3
  formalization ~12:40am, Run 11 at iter-2 formalization ~7:50pm).
  Configure both providers; plan for off-peak windows.
- **Run 11 specifically** ran iter-0 (Run 15: $55.30 PROVE producing the
  6→1 keystone narrowing on `backup/run15-progress-2ce1245`), then iter-1
  (Run 16: $29.27 PROVE producing the structured ~16-sorry
  `TermFundamental.lean` skeleton + architectural-impossibility diagnosis
  on `backup/run16-progress-3234c92`), then iter-2 died on quota at
  `[19:48:26]`. Master advanced only via the iter-0 escalation cosmetic
  attic Groupoids merge (`784c5f8 → 97db50a`). Both committed frontier
  advances are preserved durably.
- **Dangling commit recovery.** Runs 8/9 both had branch-deletion incidents
  on worktree teardown. **Always set `backup/run23-progress-<sha>` before
  any destructive op.** Current preserved backup refs:
  `backup/run21-progress-f945b1a` (latest keystone frontier with whred
  helpers), `backup/run20-progress-b2d7cc3` (`TermLRApp` pre-merge),
  `backup/run19-progress-46ede3d` (`TermLRKripke` pre-merge),
  `backup/run16-progress-3234c92`, `backup/run15-progress-2ce1245`,
  `backup/run14-progress-22b1755`, etc.
- **NFS git lock contention.** Run 12's escalation pass spawned two
  concurrent worktree-deletion loops fighting over the same git locks,
  leaving stale `index.lock` files. If running parallel worktree git ops,
  serialize them or run `find .git/worktrees -name index.lock -delete`
  between passes. NFS-backed git commits routinely take 30–60 s each.
- **Git worktree leak.** Repair: `git branch -m worktree/<name>
  backup/<name>-orphan-<sha>`, `rm -rf .worktrees/<name>`, `git worktree
  prune`.
- **OOM kill.** Slurm cgroup ~2 GB anon-rss. Not state corruption.
- **Lake module names:** `HoTTLean.Syntax.LogicalRelation.X` (file-path
  namespace), NOT `SynthLean.X` (the Lean `namespace`).
- **Critic post-run audits** consistently report "no commits on branch
  worktree/chunk-0-* beyond main" — this is expected (those chunks are
  already on master sorry-free); do not act on it.
- **Escalation phase is a sorry-token sweep** producing 1000+
  ILLEGITIMATE-sorry warnings from `attic/*` and `test/*` files that import
  a nonexistent `GroupoidModel` package or rest on a Mathlib-bump-broken
  Groupoids chain. All out-of-scope; do not chase them. Run 13's
  escalation phase correctly triaged this as a no-op token sweep.
- **Run 12's escalation pass** attempted to convert the standing "recommend
  DROP" verdict into ACTION by deleting the 20 dead `attic/*` + `test/unitt.lean`
  files via per-worktree `git rm` + `UNITY: merge chunk` to master. 3–4
  deletions landed (`attic/Display.lean`,
  `attic/FibrationForMathlib/Displayed/Fibration.lean`,
  `attic/FibrationForMathlib/FibredCats/CartLift.lean`) before the SDK
  idle-timeout killed the harness mid-merge (NFS-slow). **`test/basic.lean:89`
  is INTENTIONAL** — it's a `#guard_msgs` test exercising the sorry-warning
  path; do NOT remove it. The remaining ~16 worktree branches carry their
  removal commit if a future run wants to finish the cleanup, but **this is
  cosmetic, not deliverable progress** — focus on the keystone.

## Source material

- `logrel-coq/theories/TypingProperties/TypeInjectivityConsequences.v` — 3 target corollaries
- `logrel-coq/theories/TypingProperties/LogRelConsequences.v` — `_ty_conv_inj`
- **`logrel-coq/theories/Fundamental.v` — the Fundamental theorem (THIS is what discharges the two named oracles)**
- `logrel-coq/theories/LogicalRelation/` — full reducible-head LR (~1200 LOC)
- `logrel-coq/theories/LogicalRelation/Symmetry.v` — historical `indLR`/`symLR_rec`;
  reference for the bidirectional-IH pattern at the term level only (not the
  active route).

## Prior run logs (for diagnostic context, not source material)

- **`unity-15.out` — SYMMETRY PROGRAM BUILD-VERIFIED DEAD AS KEYSTONE
  PATH (KERNEL NON-POSITIVITY + DEPENDENCY-MAP ORTHOGONALITY); LIVE
  ROUTE PIVOTED TO MUTUAL-RECURSION TERMINATION MEASURE.**
  Three-iteration superrun (≈ $103 total). Master UNCHANGED at
  `d290325`; `git log d290325..HEAD` empty across all 3 iterations.
  iter-0 Run 30 PROVE ($19.5): committed `49c9acb` —
  `MergedLR.symLR_rec` skeleton with `univ`/`ne`/`Id` cases CLOSED
  sorry-free; `pi`/`sigma` each reduced to a single per-arg
  `LRtmEqWM` swap obligation (the `symRedTm` / `RT Δ ξ b a` witness).
  Build green-modulo-2-sanctioned-spine-sorries (131 jobs). Backup
  `backup/run30-progress-49c9acb` (22 ahead).
  iter-1 Run 31 PROVE ($17.6): TWO build-verified architectural
  results across 2 sequential metatheory-prover passes. (a) Committed
  `82d8c5c` — **REFUTATION** of the Run-30 additive `adRTinv`
  inverse-adequacy field mandate: adding
  `(∀ {Δ ξ a b}, Wk E Δ Γ ξ → RT Δ ξ a b → adRT Δ ξ b a)` to
  `MergedLR.pi/sigma` is kernel-impossible
  (`arg #22 of LRtyEqM.pi has a non positive occurrence`). The
  refutation generalizes to ALL additive biconditional fields on the
  abstract-`RT` pack — symmetry requires *constructing* an `RT` from
  a term witness, but abstract `RT` is write-only, so any bridge
  forces `LRtmEqWM` negative. (b) Committed `56be3ab` —
  **build-verified dependency map** proving NONE of the 14
  `TermFundamental.lean` keystone sorries needs the merged-block
  `LRtyEqM.symm`; they are all blocked on mutual `WfTm`/`EqTm`/
  Fundamental induction (`Prop`-`SizeOf≡0` termination, the Run-16
  issue) — orthogonal to the entire 30-run symmetry program. Plus new
  sorry-free `LRtmEqWM.conv_dom` lemma (`MergedLR.lean:272`). Backup
  `backup/run31-progress-56be3ab` (24 ahead — new seed).
  iter-2 Run 32 ($29.9): no PROVE (3-iter cap exhausted on critic
  loop). Run 32 critic: NEEDS_REVISION case (a) — committed worktree
  progress exists; master is byte-identical to Runs 27–31 baselines.
  Strategic redirect (decision `2a4c4942`): stop chasing merged-block
  type symmetry; live route is category-D
  (`conv_eq`/`refl_tm`/`symm_tm'`/`trans_tm'` mutual-recursion
  termination measure) or category-A/B (head-term reducibility via
  forward forgetful maps + `LRtmEqWM.conv_dom`). Retro captured: the
  symmetry program is now provably DEAD across two independent
  refutations.
- **`unity-14.out` — 3 SUCCESSIVE SORRY-FREE IN-BLOCK DECOUPLING ADVANCES
  ON TOP OF `MergedLR`; CONSUMER PORT CONCRETELY ADVANCING.**
  Three-iteration superrun (≈ $110 total). Master advanced
  `3052fb3 → d290325` via Run 29 escalation iter-1 cosmetic
  fix to `test/basic.lean:89` (rewrote the deliberate `mltt def foo :
  Type := sorry` `#guard_msgs` fixture to `:= B`, preserving the
  warning test while satisfying strict zero-sorry policy). The 3
  axioms remain intact.
  iter-0 Run 27 PROVE ($14.5): committed `c4c6a0b` — first genuine
  downstream consumer of `MergedLR` (`URedEq.cong_ne_intro` routed
  through new unconditional `URedEqM.ne_forget` /
  `LRtmEqWM.forget_nonuniv`). Backup `backup/run27-progress-c4c6a0b`
  (18 ahead).
  iter-1 Run 28 PROVE ($15.0): committed `064924e` — `URedEq.bvar_intro`
  and `ax_intro` consumer port via `ne_forget`. Build-verified WALL-1:
  symm/trans of `MergedLR` via a single generic `mutual_induction
  LRtyEqM` is REFUTED (abstract `RT` pack is positive-only); needs
  bespoke `symLR_rec`. Backup `backup/run28-progress-064924e` (19
  ahead).
  iter-2 Run 29 PROVE ($32.65): 2 sequential metatheory-prover passes.
  Committed `318d648` — closed `idRec_refl'` redex case of
  `fundamental_valid` **in-block** via `TmStep.idRec_refl` +
  `Expr.subst_snoc_toSb_subst`. Committed `5478333` — new sorry-free
  reusable `URedEq.code_el_code` (universe-η `redSubst` at a code
  head) closes the **code branch of `code_el`** in-block; surviving
  wall is the η-mismatch *neutral* branch (genuinely needs the merged
  reorg, confirming forward port is load-bearing). Backups
  `backup/run29-progress-318d648` (20 ahead) and
  `backup/run29b-progress-5478333` (21 ahead — the new seed). Critic
  Run 29 verdict: NEEDS_REVISION case (a) (committed-advance review).
  Retro captured: in-block decoupling pattern; symLR_rec note.
- **`unity-13.out` — KERNEL-ACCEPTED MERGED INDUCTIVE `MergedLR.lean`
  SORRY-FREE; CONSUMER PORT 2-OF-3 DIRECTIONS CLOSED SORRY-FREE;
  BACKWARD-BRIDGE REFUTED → ROUTE PINNED FORWARD-ONLY.** Multi-iteration
  superrun. iter-0/1 confirmed master at `3052fb3` (Run 25 escalation
  pass completed: 9 attic deletions landed; `test/basic.lean:89`
  preserved). Critic Run 25 verdict: NEEDS_REVISION (deliverable unmet,
  3 axioms intact). iter-2 ran Run 26 PROVE ($48): seeded chunk-1-2
  worktree from `backup/run25-progress-1b147f6`, committed two
  sorry-free advances:
  (1) `57d1f13` parameter-free `MergedLR` escape layer via single
  genuine `mutual_induction LRtyEqM` (escape_conj + escapeEqM/
  escapeEqTermM/escapeURedEqM); first structurally-recursive consumer
  of MergedLR — proves the merged block is usable, not merely
  kernel-acceptable;
  (2) `1e8f4d9` consumer port: `URedEqM → URedEq` and `LRtmEqWM →
  LRtmEqW` close sorry-free (11/13 cases discharge mechanically),
  while `LRtyEqM → LRtyEq` is **build-verified REFUTED** (abstract RT
  pack has no introduction form into existing RedTysW-based pi/sigma
  fields). Backup `backup/run26-progress-1e8f4d9` set (17 ahead of
  master). Critic Run 27 verdict: NEEDS_REVISION case (a) committed
  advance + sharpened forward-only route diagnosis. Retro recorded
  the forward-only finding in
  `~/.unity/library/tactics/type-theory.md` and project notes. API
  quota exhausted during a subsequent critic iteration.
- **`unity-12.out` — KEYSTONE NARROWED VIA WHRED-EXPANSION HELPERS;
  POSRED-CARRIES-LRTMEQW ROUTE PINNED.** Three-iteration superrun.
  iter-0/1 confirmed master at `1ac026b` (carrying Run-19 `TermLRKripke`
  and Run-20 `TermLRApp` Kripke bricks); critic Run 22 verdict
  NEEDS_REVISION case (a). iter-2 ran Run 21 PROVE ($56): seeded chunk-1-2
  worktree from `backup/run20-progress-b2d7cc3`, added 4 sorry-free
  whred-expansion discharge helpers (`URedEq.{app_lam,fst_pair,snd_pair,
  idRec_refl}_whred`) taking explicit reduct-canonicity, wired the 4
  category-(B) `URedEq.fundamentalAux` arms through them, narrowing each
  residual `sorry` to `URedEq r r`. Backup
  `backup/run21-progress-f945b1a` set (7 ahead of master). Critic Run 22:
  NEEDS_REVISION case (a) — committed advance, not intractable.
  Retrospective recorded the helper pattern in
  `~/.unity/library/tactics/type-theory.md` and the project notes.
  Escalation iter-2 attempted to delete 20 dead `attic/*` + `test/unitt.lean`
  files (preserving intentional `test/basic.lean:89`) via per-worktree git
  rm + UNITY squash-merge; the SDK idle-timeout killed the harness with
  3–4 deletions landed on master; remaining ~16 worktree branches carry
  their removal commit unmerged. Total spend ≈ $80 before harness death.
- **`unity-11.out` — STANDALONE-KEYSTONE FRAMING REFUTED ON ARCHITECTURAL
  GROUNDS.** Three-iteration superrun. iter-0 ran Run 15 ($55.30 PROVE)
  which collapsed the 6 Run-14 funnel-sorries in `Fundamental.lean` into
  **ONE** named keystone sorry at `TermFundamental.lean:53` on
  `backup/run15-progress-2ce1245` (`Fundamental.lean` fully sorry-free).
  iter-1 ran Run 16 ($29.27 PROVE) which attempted to discharge the
  standalone keystone, produced a structured ~16-sorry skeleton on
  `backup/run16-progress-3234c92`, and **diagnosed the two architectural
  walls** ruling out the standalone approach: (1) import cycle — the
  keystone needs type-side IHs only available downstream; (2) `Prop`-`SizeOf`
  degeneracy — Lean auto-derives `SizeOf P ≡ 0` for `Prop`, killing
  standalone well-founded recursion. Master advanced only by ONE attic
  Groupoids cosmetic escalation merge (`784c5f8 → 97db50a`). Critic verdict
  NEEDS_REVISION case (a) committed advance + sharpened architectural
  diagnosis. iter-2 died at `[19:48:26]` on `You're out of extra usage ·
  resets 9:50pm`.
- **`unity-10.out` — KEYSTONE INDUCTION FIRST ASSEMBLED.** Two-iteration
  superrun: iter-0 ran Run 13 ($45.06 PROVE) landing `RedSubst.lean` (VRU
  refined env + `bvar_motive` leaf) and `TermLR.lean` (`LRtmEqW` term-level
  reducible equality, 5 cases + escape/symm/rename bridges) sorry-free to
  master `af77bb5 → c91ce7f → 784c5f8`. iter-1 ran Run 14 ($56.09 PROVE)
  which — for the first time across project history — **assembled the
  `mutual_induction WfCtx` simultaneous-induction skeleton
  `fundamental_valid`** on `worktree/chunk-1-2` /
  `backup/run14-progress-22b1755` with **21 of 27 constructor cases
  sorry-free + axiom-clean** (initially 7 sorries, narrowed to 6 by
  decoupling lookup-slot from universe-contents level in `RedSubst`'s
  `slot_ured`/`bvar_motive`). 6 residual sorries all funnel to a single named
  blocker, `URedEq.fundamental` (term-universe canonicity). Branch NOT
  merged (sorry-free invariant). Critic verdict NEEDS_REVISION case (a)
  committed advance. iter-2 died at `[22:35:34]` on `You're out of extra
  usage · resets 12:40am`.
- `unity-9.out` — first master advance since Run 3: `03a7810 → af77bb5`,
  26 commits ahead of origin. Long chain of `UNITY: merge chunk …` commits
  merged the `LRtyEq`→`RedTysW` migration plus
  `RedStable`/`ElBridge`/`UnivReducible`/`UnivFundamental`/
  `UnivFundamentalThm`/`ElCodeBranch` development directly to master,
  sorry-free. `conv_inj_red` (conditional `ty_conv_inj`) present in
  `Consequences.lean` sorry-free. Escalation closed
  `HoTTLean/ForPoly.lean` `snd'_verticalNatTrans_app` (Beck–Chevalley
  vertical) sorry-free.
- `unity-8.out` — WALL-1 dissolution: genuine-pack `LRtyAtW` as `def` via
  `WellFounded.fix` on `sizeOf W`. `LRtyAtW_symm`/`_trans` sorry-free for
  all heads. New files `LRWf.lean`/`LRWfConvCtx.lean`/`LRWfClosed.lean` —
  **lived ONLY on `run8-landed` and intra-Run-9-iteration frontier
  branches, never reached master**; Run 9 routed around differently
  (relational `RedTysW` engine + named Prop oracles).
- `unity-7.out` — refuted single-argument-posRed from Coq; landed
  `congPiTyM`/`congSigmaTyM`. Superseded by Run 8/9.
- `unity-1.out`–`unity-6.out` — abstract-pack LR library build-out,
  superseded.
- `REPORT.md` — most recent critic verdict (NEEDS_REVISION, case (a)).
- `ESCALATED.md`, `.unity/notes.md`, `.unity/sorry-log.md`,
  `.unity/tactics.md` — accumulated project knowledge (notes.md and
  sorry-log.md updated through Run 14).
- `~/.unity/library/ir-patterns/logrel-coq-type-injectivity.md` — global
  library IR pattern (updated through Run 14 with simultaneous-induction
  assembly note).
- `~/.unity/library/tactics/type-theory.md` — global library tactics (876
  lines; updated through Run 14 with **VRU**/`LRtmEqW` patterns and the
  bvar level-decoupling lesson).
- `~/.unity/library/subagents/type-theory-metatheory-prover.md` — subagent
  template with named-oracle landing strategy.
