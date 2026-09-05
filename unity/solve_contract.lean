/-
SOLVE-only kernel contract and axiom extractor. This is deliberately independent of
the human-readable blueprint extractor, and never applies its name/noise filters.

After building all project modules:
  lake env lean --run <this file> Module.One Module.Two -- Target.one Target.two

The module arguments define project ownership; callers MUST separately pin/audit
all external dependencies and the toolchain. Output is a JSON object containing
`targets` (keyed by exact declaration name), `issues`, and whole-project
`project_axioms`/`project_sorries` lists. The latter list records direct sorryAx
uses in any project declaration's type or value, so even unused private proof
holes are visible. These lists are final-completion checks, not frozen meanings.
A target has `name`,
`target_kind`, `module`, `level_params`, structural `type`, project-owned
`meanings`, and full-environment `axioms`. Only `axioms` is proof-dependent.
The caller hashes the other fields, not the entire record including `axioms`.

Expressions, names, and universe levels use tagged structural JSON, never pretty
printing. Non-kernel Expr.mdata annotations are erased; all other constructors,
binders, indices, and levels are preserved. Meanings follow types and definition
values (including opaque values), inductive constructors and recursor rules, but
never theorem proof bodies. A def/opaque target's own value is protected too.
The separate axiom traversal DOES follow every proof body, including external,
generated, and private declarations loaded with importAll. Dependency edges are
memoized across targets; visited sets make recursive inductive graphs finite.
Missing targets/constants, bad arguments, and import failures fail closed.
-/
import Lean

open Lean

namespace UnitySolveContract

private def tag (s : String) (xs : Array Json := #[]) : Json :=
  Json.arr (#[Json.str s] ++ xs)

private def natJson (n : Nat) : Json := toJson n

private def nameJson : Name → Json
  | .anonymous => tag "anonymous"
  | .str p s => tag "str" #[nameJson p, Json.str s]
  | .num p n => tag "num" #[nameJson p, natJson n]

private def namesJson (ns : List Name) : Json := Json.arr (ns.toArray.map nameJson)

private def levelJson : Level → Json
  | .zero => tag "zero"
  | .succ u => tag "succ" #[levelJson u]
  | .max u v => tag "max" #[levelJson u, levelJson v]
  | .imax u v => tag "imax" #[levelJson u, levelJson v]
  | .param n => tag "param" #[nameJson n]
  | .mvar n => tag "mvar" #[nameJson n.name]

private def binderJson : BinderInfo → Json
  | .default => Json.str "default"
  | .implicit => Json.str "implicit"
  | .strictImplicit => Json.str "strictImplicit"
  | .instImplicit => Json.str "instImplicit"

private partial def exprJson : Expr → Json
  | .bvar i => tag "bvar" #[natJson i]
  | .fvar n => tag "fvar" #[nameJson n.name]
  | .mvar n => tag "mvar" #[nameJson n.name]
  | .sort u => tag "sort" #[levelJson u]
  | .const n us => tag "const" #[nameJson n, Json.arr (us.toArray.map levelJson)]
  | .app f a => tag "app" #[exprJson f, exprJson a]
  | .lam n t b bi => tag "lam" #[nameJson n, exprJson t, exprJson b, binderJson bi]
  | .forallE n t b bi => tag "forallE" #[nameJson n, exprJson t, exprJson b, binderJson bi]
  | .letE n t v b nd => tag "letE" #[nameJson n, exprJson t, exprJson v, exprJson b, Json.bool nd]
  | .lit (.natVal n) => tag "natVal" #[natJson n]
  | .lit (.strVal s) => tag "strVal" #[Json.str s]
  | .mdata _ e => exprJson e
  | .proj n i e => tag "proj" #[nameJson n, natJson i, exprJson e]

/-- Lean's getUsedConstants omits projection type names. Include those as well,
    with a cached expression visitor so shared proof DAGs are visited only once. -/
private def usedConstants (e : Expr) : Array Name := runST fun σ => do
  let names : ST.Ref σ NameSet ← ST.mkRef {}
  e.forEach (ω := σ) (m := ST σ) fun node => match node with
    | .const n _ | .proj n _ _ => names.modify (·.insert n)
    | _ => pure ()
  return (← names.get).toList.toArray

private def kindOf : ConstantInfo → String
  | .thmInfo _ => "theorem"
  | .defnInfo _ => "def"
  | .axiomInfo _ => "axiom"
  | .opaqueInfo _ => "opaque"
  | .inductInfo _ => "inductive"
  | .ctorInfo _ => "constructor"
  | .recInfo _ => "recursor"
  | .quotInfo _ => "quot"

private def moduleOf (env : Environment) (n : Name) : Option Name := do
  let idx ← env.getModuleIdxFor? n
  env.header.moduleNames[idx.toNat]?

private def hintsJson : ReducibilityHints → Json
  | .opaque => tag "opaque"
  | .abbrev => tag "abbrev"
  | .regular h => tag "regular" #[natJson h.toNat]

private def safetyJson : DefinitionSafety → Json
  | .safe => Json.str "safe"
  | .unsafe => Json.str "unsafe"
  | .partial => Json.str "partial"

private def ruleJson (r : RecursorRule) : Json := Json.mkObj [
  ("ctor", nameJson r.ctor), ("nfields", natJson r.nfields), ("rhs", exprJson r.rhs)]

private def meaningJson (env : Environment) (ci : ConstantInfo) : Json :=
  let base := [
    ("name", nameJson ci.name),
    ("kind", Json.str (kindOf ci)),
    ("module", Json.str (((moduleOf env ci.name).getD .anonymous).toString)),
    ("level_params", namesJson ci.levelParams),
    ("type", exprJson ci.type)]
  let extra := match ci with
    | .axiomInfo v => [("unsafe", Json.bool v.isUnsafe)]
    | .defnInfo v => [
        ("value", exprJson v.value), ("hints", hintsJson v.hints),
        ("safety", safetyJson v.safety), ("all", namesJson v.all)]
    | .thmInfo _ => [] -- Proof replacement must not change the frozen statement.
    | .opaqueInfo v => [
        ("value", exprJson v.value), ("unsafe", Json.bool v.isUnsafe),
        ("all", namesJson v.all)]
    | .inductInfo v => [
        ("num_params", natJson v.numParams), ("num_indices", natJson v.numIndices),
        ("all", namesJson v.all), ("ctors", namesJson v.ctors),
        ("num_nested", natJson v.numNested), ("recursive", Json.bool v.isRec),
        ("unsafe", Json.bool v.isUnsafe), ("reflexive", Json.bool v.isReflexive)]
    | .ctorInfo v => [
        ("induct", nameJson v.induct), ("index", natJson v.cidx),
        ("num_params", natJson v.numParams), ("num_fields", natJson v.numFields),
        ("unsafe", Json.bool v.isUnsafe)]
    | .recInfo v => [
        ("all", namesJson v.all), ("num_params", natJson v.numParams),
        ("num_indices", natJson v.numIndices), ("num_motives", natJson v.numMotives),
        ("num_minors", natJson v.numMinors),
        ("rules", Json.arr (v.rules.toArray.map ruleJson)),
        ("k", Json.bool v.k), ("unsafe", Json.bool v.isUnsafe)]
    | .quotInfo v => [("quot_kind", Json.str (match v.kind with
        | .type => "type" | .ctor => "ctor" | .lift => "lift" | .ind => "ind"))]
  Json.mkObj (base ++ extra)

/-- Semantic edges: no theorem proof bodies; generated recursors are not filtered. -/
private def meaningDeps (env : Environment) (ci : ConstantInfo) : Array Name := Id.run do
  let mut deps := usedConstants ci.type
  match ci with
  | .defnInfo v => deps := deps ++ usedConstants v.value
  | .opaqueInfo v => deps := deps ++ usedConstants v.value
  | .inductInfo v =>
    deps := deps ++ v.all.toArray ++ v.ctors.toArray
    for n in v.all do
      let recName := n.appendCore `rec
      if (env.checked.get.find? recName).isSome then deps := deps.push recName
  | .ctorInfo v => deps := deps.push v.induct
  | .recInfo v =>
    deps := deps ++ v.all.toArray
    for r in v.rules do deps := (deps.push r.ctor) ++ usedConstants r.rhs
  | _ => pure ()
  return deps

private def auditDeps (env : Environment) (ci : ConstantInfo) : Array Name :=
  let deps := meaningDeps env ci
  match ci with
  | .thmInfo v => deps ++ usedConstants v.value
  | _ => deps

private structure AuditState where
  edges : NameMap (Array Name) := {}
  visited : NameSet := {}
  axioms : NameSet := {}
  issues : Array String := #[]

private partial def audit (env : Environment) (n : Name) : StateM AuditState Unit := do
  if (← get).visited.contains n then return
  modify fun s => { s with visited := s.visited.insert n }
  let some ci := env.checked.get.find? n | do
    modify fun s => { s with issues := s.issues.push s!"constant {n} missing during axiom audit" }
    return
  if let .axiomInfo _ := ci then
    modify fun s => { s with axioms := s.axioms.insert n }
  let deps ← match (← get).edges.find? n with
    | some deps => pure deps
    | none => do
      let deps := auditDeps env ci
      modify fun s => { s with edges := s.edges.insert n deps }
      pure deps
  for dep in deps do audit env dep

private structure MeaningState where
  visited : NameSet := {}
  records : List (String × Json) := []
  issues : Array String := #[]

private partial def collectMeanings (env : Environment) (projects : NameSet)
    (n : Name) : StateM MeaningState Unit := do
  if (← get).visited.contains n then return
  modify fun s => { s with visited := s.visited.insert n }
  let some ci := env.checked.get.find? n | do
    modify fun s => { s with issues := s.issues.push s!"constant {n} missing during meaning audit" }
    return
  unless (moduleOf env n).any projects.contains do return
  modify fun s => { s with records := (n.toString, meaningJson env ci) :: s.records }
  for dep in meaningDeps env ci do collectMeanings env projects dep

private def extract (modules targets : List String) : IO (Json × UInt32) := do
  initSearchPath (← findSysroot)
  let mods := modules.toArray.map fun a => ({ module := a.toName, importAll := true } : Import)
  let env ← importModules mods {} 0
  let projects : NameSet := modules.foldl (fun s a => s.insert a.toName) {}
  let mut projectAxioms : List String := []
  let mut projectSorries : List String := []
  for (n, ci) in env.constants.toList do
    unless (moduleOf env n).any projects.contains do continue
    if let .axiomInfo _ := ci then projectAxioms := n.toString :: projectAxioms
    let direct := usedConstants ci.type ++
      ((ci.value? (allowOpaque := true)).map usedConstants).getD #[]
    if direct.contains ``sorryAx then projectSorries := n.toString :: projectSorries
  let mut records : List (String × Json) := []
  let mut issues : Array String := #[]
  let mut edges : NameMap (Array Name) := {}
  for target in targets do
    let n := target.toName
    let some ci := env.checked.get.find? n | do
      issues := issues.push s!"target declaration {target} was not found in the built kernel"
      continue
    unless (moduleOf env n).any projects.contains do
      issues := issues.push s!"target declaration {target} is not owned by a supplied project module"
      continue
    let roots := match ci with
      | .defnInfo _ | .opaqueInfo _ => (usedConstants ci.type).push n
      | _ => usedConstants ci.type
    let (_, ms) := (roots.forM (collectMeanings env projects)).run {}
    let (_, auditState) := (audit env n).run { edges := edges }
    edges := auditState.edges
    issues := issues ++ ms.issues ++ auditState.issues
    let axiomNames := auditState.axioms.toList.map Name.toString |>.mergeSort (· ≤ ·)
    records := (target, Json.mkObj [
      ("name", Json.str ci.name.toString),
      ("target_kind", Json.str (kindOf ci)),
      ("module", Json.str (((moduleOf env n).getD .anonymous).toString)),
      ("level_params", namesJson ci.levelParams),
      ("type", exprJson ci.type),
      ("meanings", Json.mkObj ms.records),
      ("axioms", Json.arr (axiomNames.toArray.map Json.str))]) :: records
  return (Json.mkObj [("targets", Json.mkObj records),
    ("project_axioms", toJson (projectAxioms.mergeSort (· ≤ ·))),
    ("project_sorries", toJson (projectSorries.mergeSort (· ≤ ·))),
    ("issues", Json.arr (issues.map Json.str))], if issues.isEmpty then 0 else 1)

end UnitySolveContract

def main (args : List String) : IO UInt32 := do
  let modules := args.takeWhile (· != "--")
  let rest := args.dropWhile (· != "--")
  let targets := rest.drop 1
  if modules.isEmpty || targets.isEmpty then
    IO.println (Json.mkObj [("targets", Json.mkObj []), ("issues", toJson [
      "usage: lake env lean --run solve_contract.lean Module... -- Target..."])]).compress
    return 1
  try
    let (result, code) ← UnitySolveContract.extract modules targets
    IO.println result.compress
    return code
  catch e =>
    IO.println (Json.mkObj [("targets", Json.mkObj []), ("issues", toJson [
      s!"kernel contract extraction failed: {e}"])]).compress
    return 1
