/- Bump scheduling inventory: exact raw module occurrences and direct edges.
The default output contains no expression trees and never traverses upstream
semantic closures. Optional local-meanings is a lazy checker input, not state. -/
import Lean
open Lean

namespace UnityBumpInventory

private def tag (s : String) (xs : Array Json := #[]) : Json := Json.arr (#[toJson s] ++ xs)
private def nameJson : Name → Json
  | .anonymous => tag "anonymous"
  | .str p s => tag "str" #[nameJson p, toJson s]
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
  | .default => toJson "default"
  | .implicit => toJson "implicit"
  | .strictImplicit => toJson "strictImplicit"
  | .instImplicit => toJson "instImplicit"
private partial def exprJson : Expr → Json
  | .bvar i => tag "bvar" #[toJson i]
  | .fvar n => tag "fvar" #[nameJson n.name]
  | .mvar n => tag "mvar" #[nameJson n.name]
  | .sort u => tag "sort" #[levelJson u]
  | .const n us => tag "const" #[nameJson n, Json.arr (us.toArray.map levelJson)]
  | .app f a => tag "app" #[exprJson f, exprJson a]
  | .lam n t b bi => tag "lam" #[nameJson n, exprJson t, exprJson b, binderJson bi]
  | .forallE n t b bi => tag "forallE" #[nameJson n, exprJson t, exprJson b, binderJson bi]
  | .letE n t v b nd => tag "letE" #[nameJson n, exprJson t, exprJson v, exprJson b, toJson nd]
  | .lit (.natVal n) => tag "natVal" #[toJson n]
  | .lit (.strVal s) => tag "strVal" #[toJson s]
  | .mdata _ e => exprJson e
  | .proj n i e => tag "proj" #[nameJson n, toJson i, exprJson e]
private def used (e : Expr) : Array Name := runST fun σ => do
  let names : ST.Ref σ NameSet ← ST.mkRef {}
  e.forEach (ω := σ) (m := ST σ) fun node => match node with
    | .const n _ | .proj n _ _ => names.modify (·.insert n)
    | _ => pure ()
  return (← names.get).toList.toArray
private def kindOf : ConstantInfo → String
  | .thmInfo _ => "theorem" | .defnInfo _ => "def" | .axiomInfo _ => "axiom"
  | .opaqueInfo _ => "opaque" | .inductInfo _ => "inductive" | .ctorInfo _ => "constructor"
  | .recInfo _ => "recursor" | .quotInfo _ => "quot"
private def direct (ci : ConstantInfo) : Array Name := Id.run do
  let mut deps := used ci.type ++ ((ci.value? (allowOpaque := true)).map used).getD #[]
  match ci with
  | .inductInfo v => deps := deps ++ v.all.toArray ++ v.ctors.toArray
  | .ctorInfo v => deps := deps.push v.induct
  | .recInfo v =>
    deps := deps ++ v.all.toArray
    for r in v.rules do deps := (deps.push r.ctor) ++ used r.rhs
  | .defnInfo v => deps := deps ++ v.all.toArray
  | .opaqueInfo v => deps := deps ++ v.all.toArray
  | _ => pure ()
  return deps.toList.eraseDups.toArray
private def importedModule (env : Environment) (n : Name) : Option Name := do
  let idx ← env.getModuleIdxFor? n
  env.header.moduleNames[idx.toNat]?
private structure Context where
  env : Environment
  module : Name
  locals : NameMap ConstantInfo
private def Context.find? (ctx : Context) (n : Name) : Option ConstantInfo :=
  (ctx.locals.find? n).orElse fun _ => ctx.env.checked.get.find? n
private def Context.owner (ctx : Context) (n : Name) : Option Name :=
  if ctx.locals.contains n then some ctx.module else importedModule ctx.env n
private def refJson (ctx : Context) (n : Name) : IO Json := do
  let some owner := ctx.owner n | throw <| IO.userError "Unresolved direct constant ownership"
  return Json.mkObj [("module", toJson owner.toString), ("name_ast", nameJson n)]
private def rangeJson (ctx : Context) (n : Name) : Json := Id.run do
  -- A same-name imported occurrence may own the merged range extension. Never
  -- attribute that range to this raw module occurrence; use module fallback.
  if importedModule ctx.env n != some ctx.module then return Json.null
  let some r := declRangeExt.find? ctx.env n | return Json.null
  return Json.mkObj [("start_line", toJson r.range.pos.line), ("start_column", toJson r.range.pos.column),
    ("end_line", toJson r.range.endPos.line), ("end_column", toJson r.range.endPos.column)]
private def hintsJson : ReducibilityHints → Json
  | .opaque => tag "opaque" | .abbrev => tag "abbrev" | .regular h => tag "regular" #[toJson h.toNat]
private def safetyJson : DefinitionSafety → Json
  | .safe => toJson "safe" | .unsafe => toJson "unsafe" | .partial => toJson "partial"
private def ruleJson (r : RecursorRule) : Json := Json.mkObj [
  ("ctor", nameJson r.ctor), ("nfields", toJson r.nfields), ("rhs", exprJson r.rhs)]
private def meaningJson (ci : ConstantInfo) : Json :=
  let base := [("name", nameJson ci.name), ("kind", toJson (kindOf ci)),
    ("level_params", namesJson ci.levelParams), ("type", exprJson ci.type)]
  let extra := match ci with
  | .axiomInfo v => [("unsafe", toJson v.isUnsafe)]
  | .defnInfo v => [("value", exprJson v.value), ("hints", hintsJson v.hints),
      ("safety", safetyJson v.safety), ("all", namesJson v.all)]
  | .thmInfo _ => []
  | .opaqueInfo v => [("value", exprJson v.value), ("unsafe", toJson v.isUnsafe), ("all", namesJson v.all)]
  | .inductInfo v => [("num_params", toJson v.numParams), ("num_indices", toJson v.numIndices),
      ("all", namesJson v.all), ("ctors", namesJson v.ctors), ("num_nested", toJson v.numNested),
      ("recursive", toJson v.isRec), ("unsafe", toJson v.isUnsafe), ("reflexive", toJson v.isReflexive)]
  | .ctorInfo v => [("induct", nameJson v.induct), ("index", toJson v.cidx),
      ("num_params", toJson v.numParams), ("num_fields", toJson v.numFields), ("unsafe", toJson v.isUnsafe)]
  | .recInfo v => [("all", namesJson v.all), ("num_params", toJson v.numParams),
      ("num_indices", toJson v.numIndices), ("num_motives", toJson v.numMotives),
      ("num_minors", toJson v.numMinors), ("rules", Json.arr (v.rules.toArray.map ruleJson)),
      ("k", toJson v.k), ("unsafe", toJson v.isUnsafe)]
  | .quotInfo v => [("quot_kind", toJson (match v.kind with
      | .type => "type" | .ctor => "ctor" | .lift => "lift" | .ind => "ind"))]
  Json.mkObj (base ++ extra)
private partial def audit (ctx : Context) (n : Name) : StateT (NameSet × Array Json) IO Unit := do
  if (← get).1.contains n then return
  modify fun (seen, rows) => (seen.insert n, rows)
  let some ci := ctx.find? n | throw <| IO.userError "Missing constant in proof trust audit"
  if let .axiomInfo v := ci then
    let ref ← refJson ctx n
    modify fun (seen, rows) => (seen, rows.push <| Json.mkObj [
      ("reference", ref), ("display_name", toJson n.toString), ("kind", toJson "axiom"),
      ("unsafe", toJson v.isUnsafe), ("type", exprJson ci.type), ("level_params", namesJson ci.levelParams)])
  for dep in direct ci do audit ctx dep

def extract (module : String) (localMeanings : Bool) : IO Json := do
  initSearchPath (← findSysroot)
  let env ← importModules #[{ module := module.toName, importAll := true }] {} 0
  let some idx := env.getModuleIdx? module.toName | throw <| IO.userError "Requested module missing"
  let some raw := env.header.moduleData[idx.toNat]? | throw <| IO.userError "Raw module data missing"
  unless raw.constNames.size == raw.constants.size do throw <| IO.userError "Raw module array mismatch"
  let mut locals : NameMap ConstantInfo := {}
  for i in [:raw.constants.size] do
    let ci := raw.constants[i]!
    unless raw.constNames[i]! == ci.name do throw <| IO.userError "Raw occurrence name mismatch"
    if locals.contains ci.name then throw <| IO.userError "Duplicate typed name in one raw module"
    locals := locals.insert ci.name ci
  let ctx : Context := { env, module := module.toName, locals }
  let mut rows : Array Json := #[]
  for ci in raw.constants do
    let deps := direct ci
    let references ← deps.toList.mapM (refJson ctx)
    let base := [("name_ast", nameJson ci.name), ("display_name", toJson ci.name.toString),
      ("kind", toJson (kindOf ci)), ("range", rangeJson ctx ci.name),
      ("is_internal", toJson ci.name.isInternalDetail), ("direct_sorry", toJson (deps.contains ``sorryAx)),
      ("dependencies", toJson references)]
    let extra ← if localMeanings then do
      let (_, (_, axioms)) ← (audit ctx ci.name).run ({}, #[])
      pure [("meaning", meaningJson ci), ("axioms", Json.arr axioms)]
    else pure []
    rows := rows.push <| Json.mkObj (base ++ extra)
  let compiled ← env.header.moduleNames.toList.mapM fun n => return (← findOLean n).toString
  return Json.mkObj [("schema_version", toJson (1 : Nat)), ("module", toJson module),
    ("mode", toJson (if localMeanings then "local-meanings" else "index")),
    ("declaration_inventory", toJson "raw-module-constants-v1"),
    ("raw_declaration_count", toJson raw.constants.size), ("declarations", Json.arr rows),
    ("imported_modules", toJson (env.header.moduleNames.toList.map Name.toString)),
    ("compiled_modules", toJson compiled)]

def imports (filename : String) : IO Json := do
  let contents ← IO.FS.readFile filename
  let (header, state, messages) ← Parser.parseHeader (Parser.mkInputContext contents filename)
  let names := ((Elab.HeaderSyntax.imports header).toList.map (·.module)).filter (fun name => !name.isAnonymous)
  return Json.mkObj [("schema_version", toJson (1 : Nat)), ("mode", toJson "imports"),
    ("path", toJson filename), ("imports", toJson (names.map Name.toString).eraseDups),
    ("header_errors", toJson (messages.hasErrors || state.recovering))]
end UnityBumpInventory

def main (args : List String) : IO UInt32 := do
  match args with
  | [module] => IO.println (← UnityBumpInventory.extract module false).compress; return 0
  | [module, "--local-meanings"] => IO.println (← UnityBumpInventory.extract module true).compress; return 0
  | ["--imports", filename] => IO.println (← UnityBumpInventory.imports filename).compress; return 0
  | _ => throw <| IO.userError "Expected MODULE [--local-meanings] or --imports FILE"
