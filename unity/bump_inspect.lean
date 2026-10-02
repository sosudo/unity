/-
Bump's independent, structural old/target inspector. Each invocation imports
ONE actual module context. Every raw declaration occurrence in that module is inventoried,
including private and generated declarations. Semantic closure crosses external
module boundaries and follows definition/opaque bodies and inductive families,
but not theorem proof bodies. A separate traversal audits every proof's axioms.
No pretty-printed signature is used as semantic identity.
-/
import Lean

open Lean

namespace UnityBumpInspect

private def tag (s : String) (xs : Array Json := #[]) : Json :=
  Json.arr (#[Json.str s] ++ xs)

private def nameJson : Name → Json
  | .anonymous => tag "anonymous"
  | .str p s => tag "str" #[nameJson p, Json.str s]
  | .num p n => tag "num" #[nameJson p, toJson n]

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
  | .bvar i => tag "bvar" #[toJson i]
  | .fvar n => tag "fvar" #[nameJson n.name]
  | .mvar n => tag "mvar" #[nameJson n.name]
  | .sort u => tag "sort" #[levelJson u]
  | .const n us => tag "const" #[nameJson n, Json.arr (us.toArray.map levelJson)]
  | .app f a => tag "app" #[exprJson f, exprJson a]
  | .lam n t b bi => tag "lam" #[nameJson n, exprJson t, exprJson b, binderJson bi]
  | .forallE n t b bi => tag "forallE" #[nameJson n, exprJson t, exprJson b, binderJson bi]
  | .letE n t v b nd => tag "letE" #[nameJson n, exprJson t, exprJson v, exprJson b, Json.bool nd]
  | .lit (.natVal n) => tag "natVal" #[toJson n]
  | .lit (.strVal s) => tag "strVal" #[Json.str s]
  | .mdata _ e => exprJson e
  | .proj n i e => tag "proj" #[nameJson n, toJson i, exprJson e]

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

private def importedModuleOf (env : Environment) (n : Name) : Option Name := do
  let idx ← env.getModuleIdxFor? n
  env.header.moduleNames[idx.toNat]?

/-- Imported environments merge compatible same-name theorem realizations and
may select a different body than the requested module's own occurrence. Keep its
raw ModuleData constants authoritative for both roots and local dependencies.
External names still resolve in this exact module's native imported context. -/
private structure OccurrenceContext where
  env : Environment
  module : Name
  localConstants : NameMap ConstantInfo

private def OccurrenceContext.find? (ctx : OccurrenceContext) (n : Name) : Option ConstantInfo :=
  (ctx.localConstants.find? n).orElse fun _ => ctx.env.checked.get.find? n

private def OccurrenceContext.moduleOf (ctx : OccurrenceContext) (n : Name) : Option Name :=
  if ctx.localConstants.contains n then some ctx.module else importedModuleOf ctx.env n

private def hintsJson : ReducibilityHints → Json
  | .opaque => tag "opaque"
  | .abbrev => tag "abbrev"
  | .regular h => tag "regular" #[toJson h.toNat]

private def safetyJson : DefinitionSafety → Json
  | .safe => Json.str "safe"
  | .unsafe => Json.str "unsafe"
  | .partial => Json.str "partial"

private def ruleJson (r : RecursorRule) : Json := Json.mkObj [
  ("ctor", nameJson r.ctor), ("nfields", toJson r.nfields), ("rhs", exprJson r.rhs)]

private def meaningJson (ci : ConstantInfo) : Json :=
  -- Module location is evidence, not meaning: dependencies may legitimately move.
  let base := [("name", nameJson ci.name), ("kind", Json.str (kindOf ci)),
    ("level_params", namesJson ci.levelParams), ("type", exprJson ci.type)]
  let extra := match ci with
    | .axiomInfo v => [("unsafe", Json.bool v.isUnsafe)]
    | .defnInfo v => [("value", exprJson v.value), ("hints", hintsJson v.hints),
        ("safety", safetyJson v.safety), ("all", namesJson v.all)]
    | .thmInfo _ => []
    | .opaqueInfo v => [("value", exprJson v.value), ("unsafe", Json.bool v.isUnsafe),
        ("all", namesJson v.all)]
    | .inductInfo v => [("num_params", toJson v.numParams), ("num_indices", toJson v.numIndices),
        ("all", namesJson v.all), ("ctors", namesJson v.ctors),
        ("num_nested", toJson v.numNested), ("recursive", Json.bool v.isRec),
        ("unsafe", Json.bool v.isUnsafe), ("reflexive", Json.bool v.isReflexive)]
    | .ctorInfo v => [("induct", nameJson v.induct), ("index", toJson v.cidx),
        ("num_params", toJson v.numParams), ("num_fields", toJson v.numFields),
        ("unsafe", Json.bool v.isUnsafe)]
    | .recInfo v => [("all", namesJson v.all), ("num_params", toJson v.numParams),
        ("num_indices", toJson v.numIndices), ("num_motives", toJson v.numMotives),
        ("num_minors", toJson v.numMinors), ("rules", Json.arr (v.rules.toArray.map ruleJson)),
        ("k", Json.bool v.k), ("unsafe", Json.bool v.isUnsafe)]
    | .quotInfo v => [("quot_kind", Json.str (match v.kind with
        | .type => "type" | .ctor => "ctor" | .lift => "lift" | .ind => "ind"))]
  Json.mkObj (base ++ extra)

private def meaningDeps (ctx : OccurrenceContext) (ci : ConstantInfo) : Array Name := Id.run do
  let mut deps := usedConstants ci.type
  match ci with
  | .defnInfo v => deps := deps ++ usedConstants v.value ++ v.all.toArray
  | .opaqueInfo v => deps := deps ++ usedConstants v.value ++ v.all.toArray
  | .inductInfo v =>
    deps := deps ++ v.all.toArray ++ v.ctors.toArray
    for n in v.all do
      let recName := n.appendCore `rec
      if (ctx.find? recName).isSome then deps := deps.push recName
  | .ctorInfo v => deps := deps.push v.induct
  | .recInfo v =>
    deps := deps ++ v.all.toArray
    for r in v.rules do deps := (deps.push r.ctor) ++ usedConstants r.rhs
  | _ => pure ()
  return deps

private structure AuditState where
  edges : NameMap (Array Name) := {}
  visited : NameSet := {}
  axioms : NameSet := {}
  issues : Array String := #[]

private partial def audit (ctx : OccurrenceContext) (n : Name) : StateM AuditState Unit := do
  if (← get).visited.contains n then return
  modify fun s => { s with visited := s.visited.insert n }
  let some ci := ctx.find? n | do
    modify fun s => { s with issues := s.issues.push s!"constant {n} missing in proof audit" }
    return
  if let .axiomInfo _ := ci then modify fun s => { s with axioms := s.axioms.insert n }
  let deps ← match (← get).edges.find? n with
    | some deps => pure deps
    | none => do
      let deps := meaningDeps ctx ci ++ (match ci with
        | .thmInfo v => usedConstants v.value
        | _ => #[])
      modify fun s => { s with edges := s.edges.insert n deps }
      pure deps
  for dep in deps do audit ctx dep

private structure MeaningState where
  visited : NameSet := {}
  -- Display names are object keys, not semantic identities. Name.toString is
  -- not injective on all kernel Names; reject collisions before Json.mkObj
  -- would silently collapse them in its underlying string-keyed map.
  displayKeys : NameSet := {}
  records : List (String × Json) := []
  issues : Array String := #[]

private partial def collectMeanings (ctx : OccurrenceContext) (n : Name) : StateM MeaningState Unit := do
  if (← get).visited.contains n then return
  modify fun s => { s with visited := s.visited.insert n }
  let some ci := ctx.find? n | do
    modify fun s => { s with issues := s.issues.push s!"constant {n} missing in semantic closure" }
    return
  let display := n.toString
  let displayKey := Name.str .anonymous display
  if (← get).displayKeys.contains displayKey then
    modify fun s => { s with issues := s.issues.push s!"distinct structural names share native display key: {display}" }
    return
  modify fun s => { s with displayKeys := s.displayKeys.insert displayKey }
  -- Deliberately no project-module boundary check here.
  let deps := meaningDeps ctx ci
  let record := Json.mkObj [("meaning", meaningJson ci),
    ("module", toJson (((ctx.moduleOf n).getD .anonymous).toString)),
    ("dependencies", toJson (deps.toList.map Name.toString |>.mergeSort (· ≤ ·)))]
  modify fun s => { s with records := (display, record) :: s.records }
  for dep in deps do collectMeanings ctx dep

def extract (module : String) (owned : List String) (pathsOnly : Bool) : IO Json := do
  initSearchPath (← findSysroot)
  let env ← importModules #[{ module := module.toName, importAll := true }] {} 0
  let compiled ← env.header.moduleNames.toList.mapM fun n => do
    return (← findOLean n).toString
  let some moduleIdx := env.getModuleIdx? module.toName |
    throw <| IO.userError s!"requested module {module} missing in native module data"
  let some raw := env.header.moduleData[moduleIdx.toNat]? |
    throw <| IO.userError s!"requested module {module} has no raw constant inventory"
  unless raw.constNames.size == raw.constants.size do
    throw <| IO.userError s!"requested module {module} has inconsistent raw constant arrays"
  let mut localConstants : NameMap ConstantInfo := {}
  for i in [:raw.constants.size] do
    let ci := raw.constants[i]!
    unless raw.constNames[i]! == ci.name do
      throw <| IO.userError s!"requested module {module} has inconsistent raw constant names"
    if localConstants.contains ci.name then
      throw <| IO.userError s!"duplicate structural declaration within raw module {module}: {ci.name}"
    localConstants := localConstants.insert ci.name ci
  let ctx : OccurrenceContext := { env, module := module.toName, localConstants }
  let base := [("schema_version", toJson (2 : Nat)), ("module", toJson module),
    ("declaration_inventory", toJson "raw-module-constants-v1"),
    ("raw_declaration_count", toJson raw.constants.size),
    ("owned_modules", toJson owned), ("compiled_modules", toJson compiled),
    ("imported_modules", toJson (env.header.moduleNames.toList.map Name.toString))]
  if pathsOnly then return Json.mkObj base
  let mut declarations : List (String × Json) := []
  let mut declarationDisplayKeys : NameSet := {}
  let mut issues : Array String := #[]
  let mut edges : NameMap (Array Name) := {}
  let mut meanings : MeaningState := {}
  for ci in raw.constants do
    let n := ci.name
    let display := n.toString
    let displayKey := Name.str .anonymous display
    if declarationDisplayKeys.contains displayKey then
      issues := issues.push s!"distinct declarations share native display key: {display}"
      continue
    declarationDisplayKeys := declarationDisplayKeys.insert displayKey
    let (_, as) := (audit ctx n).run { edges := edges }
    edges := as.edges
    issues := issues ++ as.issues
    -- Audit axiom *types* as meanings too: a same-named assumption may change.
    let roots := #[n] ++ as.axioms.toList.toArray
    let (_, next) := (roots.forM (collectMeanings ctx)).run meanings
    meanings := next
    let direct := usedConstants ci.type ++
      ((ci.value? (allowOpaque := true)).map usedConstants).getD #[]
    declarations := (display, Json.mkObj [("name", toJson display),
      ("module", toJson module), ("kind", toJson (kindOf ci)),
      ("is_internal_detail", toJson n.isInternalDetail),
      ("direct_sorry", toJson (direct.contains ``sorryAx)),
      ("axioms", toJson (as.axioms.toList.map Name.toString |>.mergeSort (· ≤ ·)))]) :: declarations
  return Json.mkObj (base ++ [("declarations", Json.mkObj declarations),
    ("meanings", Json.mkObj meanings.records),
    ("issues", toJson (issues ++ meanings.issues))])

end UnityBumpInspect

def main (args : List String) : IO UInt32 := do
  let pathsOnly := args.contains "--paths-only"
  let args := args.filter (· != "--paths-only")
  match args with
  | module :: "--owned" :: owned =>
    try
      let result ← UnityBumpInspect.extract module owned pathsOnly
      IO.println result.compress
      return 0
    catch e =>
      IO.println (Json.mkObj [("issues", toJson [s!"Bump native inspection failed: {e}"]) ]).compress
      return 1
  | _ =>
    IO.println (Json.mkObj [("issues", toJson ["usage: inspector Module --owned Module..."]) ]).compress
    return 1
