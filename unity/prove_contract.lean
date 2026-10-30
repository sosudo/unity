/- Prove-only structural kernel target contracts and exact candidate assignments. -/
import Lean
open Lean
set_option maxRecDepth 8192
private def tag (s : String) (xs : Array Json := #[]) : Json := Json.arr (#[Json.str s] ++ xs)
private def nameJson : Name → Json
  | .anonymous => tag "anonymous"
  | .str p s => tag "str" #[nameJson p, Json.str s]
  | .num p n => tag "num" #[nameJson p, toJson n]
private def levelJson : Level → Json
  | .zero => tag "zero"
  | .succ a => tag "succ" #[levelJson a]
  | .max a b => tag "max" #[levelJson a, levelJson b]
  | .imax a b => tag "imax" #[levelJson a, levelJson b]
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

private def parts (j : Json) (n : Nat) : Except String (Array Json) := do
  let xs ← j.getArr?
  if xs.size != n then throw "invalid structural node arity"
  return xs
private partial def parseName (j : Json) : Except String Name := do
  let xs ← j.getArr?
  let some first := xs[0]? | throw "missing name tag"
  let s ← first.getStr?
  match s with
  | "anonymous" => let _ ← parts j 1; return .anonymous
  | "str" => let xs ← parts j 3; return .str (← parseName xs[1]!) (← xs[2]!.getStr?)
  | "num" => let xs ← parts j 3; return .num (← parseName xs[1]!) (← xs[2]!.getNat?)
  | _ => throw "invalid name tag"
private partial def parseLevel (j : Json) : Except String Level := do
  let xs ← j.getArr?
  let some first := xs[0]? | throw "missing level tag"
  let s ← first.getStr?
  match s with
  | "zero" => let _ ← parts j 1; return .zero
  | "succ" => let xs ← parts j 2; return .succ (← parseLevel xs[1]!)
  | "max" => let xs ← parts j 3; return .max (← parseLevel xs[1]!) (← parseLevel xs[2]!)
  | "imax" => let xs ← parts j 3; return .imax (← parseLevel xs[1]!) (← parseLevel xs[2]!)
  | "param" => let xs ← parts j 2; return .param (← parseName xs[1]!)
  | _ => throw "invalid or metavariable level"
private def parseBinder (j : Json) : Except String BinderInfo := do
  match ← j.getStr? with
  | "default" => return .default
  | "implicit" => return .implicit
  | "strictImplicit" => return .strictImplicit
  | "instImplicit" => return .instImplicit
  | _ => throw "invalid binder"
private partial def parseExpr (j : Json) : Except String Expr := do
  let xs ← j.getArr?
  let some first := xs[0]? | throw "missing expression tag"
  let s ← first.getStr?
  match s with
  | "bvar" => let xs ← parts j 2; return .bvar (← xs[1]!.getNat?)
  | "sort" => let xs ← parts j 2; return .sort (← parseLevel xs[1]!)
  | "const" =>
    let xs ← parts j 3
    let us ← xs[2]!.getArr?
    return .const (← parseName xs[1]!) (← us.toList.mapM parseLevel)
  | "app" => let xs ← parts j 3; return .app (← parseExpr xs[1]!) (← parseExpr xs[2]!)
  | "lam" => let xs ← parts j 5; return .lam (← parseName xs[1]!) (← parseExpr xs[2]!) (← parseExpr xs[3]!) (← parseBinder xs[4]!)
  | "forallE" => let xs ← parts j 5; return .forallE (← parseName xs[1]!) (← parseExpr xs[2]!) (← parseExpr xs[3]!) (← parseBinder xs[4]!)
  | "letE" => let xs ← parts j 6; return .letE (← parseName xs[1]!) (← parseExpr xs[2]!) (← parseExpr xs[3]!) (← parseExpr xs[4]!) (← xs[5]!.getBool?)
  | "natVal" => let xs ← parts j 2; return .lit (.natVal (← xs[1]!.getNat?))
  | "strVal" => let xs ← parts j 2; return .lit (.strVal (← xs[1]!.getStr?))
  | "proj" => let xs ← parts j 4; return .proj (← parseName xs[1]!) (← xs[2]!.getNat?) (← parseExpr xs[3]!)
  | _ => throw "invalid or free/metavariable expression"

def kindOf : ConstantInfo → String
  | .thmInfo _ => "theorem" | .defnInfo _ => "def" | .axiomInfo _ => "axiom"
  | .opaqueInfo _ => "opaque" | .inductInfo _ => "inductive" | .ctorInfo _ => "constructor"
  | .recInfo _ => "recursor" | .quotInfo _ => "quot"
def isNoise (n : Name) : Bool := n.isInternal || n.components.any fun c =>
  let s := c.toString
  s.startsWith "match_" || s.startsWith "proof_" || s.startsWith "eq_" ||
  s.startsWith "injEq" || s.startsWith "sizeOf" || s.startsWith "_"

private partial def freshCheckName (env : Environment) (index : Nat) : Name :=
  let name := Name.num `_unityProveContractCheck index
  if (env.find? name).isSome then freshCheckName env (index + 1) else name

private def moduleOf (env : Environment) (n : Name) : Option Name := do
  let idx ← env.getModuleIdxFor? n
  env.header.moduleNames[idx.toNat]?

private def originJson (env : Environment) (n : Name) : Json :=
  (moduleOf env n).map (Json.str ∘ Name.toString) |>.getD Json.null

-- Unlike getUsedConstants, include the structure name stored in a projection.
private def referencedNames (e : Expr) : IO (Array Name) := do
  let found ← IO.mkRef (({} : NameSet), (#[] : Array Name))
  e.forEach fun part => do
    let n? := match part with
      | .const n _ | .proj n _ _ => some n
      | _ => none
    if let some n := n? then
      found.modify fun (seen, names) =>
        if seen.contains n then (seen, names) else (seen.insert n, names.push n)
  return (← found.get).2

private def contractOrigins (env : Environment) (targets : NameSet) (e : Expr) : IO Json := do
  let mut pending ← referencedNames e
  let mut seen : NameSet := {}
  let mut out : Array Json := #[]
  let mut index := 0
  while index < pending.size do
    let n := pending[index]!
    index := index + 1
    if seen.contains n then continue
    seen := seen.insert n
    let some ci := env.find? n | throw (IO.userError s!"type dependency missing: {n}")
    out := out.push <| Json.mkObj [("name", nameJson n), ("module", originJson env n)]
    if (moduleOf env n).map targets.contains |>.getD false then
      pending := pending ++ (← referencedNames ci.type)
      -- Trace computational definitions, not opaque theorem proof bodies.
      match ci with
      | .defnInfo value => pending := pending ++ (← referencedNames value.value)
      | .opaqueInfo value => pending := pending ++ (← referencedNames value.value)
      | _ => pure ()
  return Json.arr out

private partial def projectionNames (e : Expr) (names : NameSet := {}) : NameSet :=
  match e with
  | .proj n _ b => projectionNames b (names.insert n)
  | .app f a => projectionNames a (projectionNames f names)
  | .lam _ t b _ | .forallE _ t b _ => projectionNames b (projectionNames t names)
  | .letE _ t v b _ => projectionNames b (projectionNames v (projectionNames t names))
  | .mdata _ b => projectionNames b names
  | _ => names

private def checkOrigins (env : Environment) (target : Name) (expected : Expr)
    (c : Json) : Except String Unit := do
  match c.getObjVal? "module", c.getObjVal? "origins" with
  | .error _, .error _ => return () -- Legacy ambiguity checks also run in Python.
  | .ok owner, .ok rawOrigins =>
    if owner != originJson env target then throw "candidate declaration owning module changed"
    let origins ← rawOrigins.getArr?
    let mut seen : NameSet := {}
    for item in origins do
      let n ← parseName (← item.getObjVal? "name")
      if seen.contains n then throw "duplicate frozen constant origin"
      seen := seen.insert n
      if (env.find? n).isNone then throw s!"frozen type constant missing: {n}"
      if (← item.getObjVal? "module") != originJson env n then
        throw s!"frozen type constant origin changed: {n}"
    for n in (projectionNames expected expected.getUsedConstantsAsSet).toList do
      unless seen.contains n do throw s!"frozen type constant origin unavailable: {n}"
  | _, _ => throw "incomplete frozen module/origin metadata"

def checkContract (env : Environment) (c : Json)
    (requiredOrigins : Json := Json.null) : Except String Bool := do
  let name ← (← c.getObjVal? "decl").getStr?
  let target := name.toName
  let some ci := env.find? target | throw "candidate declaration missing"
  let rawLevels ← (← c.getObjVal? "level_params").getArr?
  let params ← rawLevels.toList.mapM parseName
  if params.length != ci.levelParams.length || params.eraseDups.length != params.length ||
      params.any (· == .anonymous) then
    throw "universe arity mismatch or duplicate universe parameters"
  let expected ← parseExpr (← c.getObjVal? "type")
  if expected.hasFVar || expected.hasMVar || expected.hasLooseBVars then
    throw "contract has free/metavariables or loose bound variables"
  checkOrigins env target expected c
  if requiredOrigins != Json.null then
    let stored ← (← c.getObjVal? "origins").getArr?
    let names ← stored.toList.mapM fun entry => do parseName (← entry.getObjVal? "name")
    for entry in (← requiredOrigins.getArr?) do
      let n ← parseName (← entry.getObjVal? "name")
      unless names.contains n do throw s!"frozen type dependency origin unavailable: {n}"
  let decl : Declaration := .defnDecl {
    name := freshCheckName env 0
    levelParams := params
    type := expected
    value := mkConst target (params.map Level.param)
    hints := .opaque
    safety := .safe
  }
  match env.addDeclCore 0 decl none with
  | .ok _ => return true
  | .error _ => return false

private def checkContractInModule (env : Environment) (targets : NameSet)
    (c : Json) : IO (Except String Bool) := do
  if (c.getObjVal? "module").isOk then
    match (c.getObjVal? "type").bind parseExpr with
    | .error e => return .error e
    | .ok expected =>
      try
        -- Recompute the closure of the frozen expression, not the candidate's
        -- type, so adding a definitionally equal alias remains permitted.
        return checkContract env c (← contractOrigins env targets expected)
      catch e => return .error s!"frozen type origins unavailable: {e}"
  return checkContract env c

def main (args : List String) : IO UInt32 := do
  initSearchPath (← findSysroot)
  let (modules, rest) := args.span (· != "--")
  if modules.isEmpty then throw (IO.userError "no modules")
  let targets : NameSet := modules.foldl (fun s a => s.insert a.toName) {}
  let contract? ← match rest with
    | ["--", path] =>
      match Json.parse (← IO.FS.readFile path) with
      | .ok c => pure (some c)
      | .error e => throw (IO.userError e)
    | [] => pure none
    | _ => throw (IO.userError "invalid arguments")
  let mut out : Array Json := #[]
  let mut ownedNames : Array Json := #[]
  let mut checks : Array (String × Json) := #[]
  for module in modules do
    -- Independent Lean modules may legitimately define the same name. Importing
    -- their union changes the project and can fail before inspection starts.
    let env ← importModules #[{ module := module.toName, importAll := true }] {} 0
    let isTarget (n : Name) : Bool := (moduleOf env n).map targets.contains |>.getD false
    for (n, ci) in env.constants.toList do
      unless moduleOf env n == some module.toName do continue
      ownedNames := ownedNames.push <| Json.mkObj [
        ("name", Json.str n.toString), ("module", Json.str module)]
      if isNoise n then continue
      let used := ci.type.getUsedConstants ++ (((ci.value? (allowOpaque := true)).map (·.getUsedConstants)).getD #[])
      let typeDeps := ci.type.getUsedConstants.filter fun d => d != n && !isNoise d
      let deps := (used.filter fun d => isTarget d && d != n && !isNoise d).toList.eraseDups
      let origins ← contractOrigins env targets ci.type
      out := out.push <| Json.mkObj [
        ("name", Json.str n.toString), ("kind", Json.str (kindOf ci)),
        ("module", Json.str module),
        ("type_repr", Json.str (toString ci.type)),
        ("type_deps", Json.arr (typeDeps.toList.eraseDups.toArray.map fun d => Json.str d.toString)),
        ("sorried", Json.bool (used.contains ``sorryAx)),
        ("deps", Json.arr (deps.toArray.map fun d => Json.str d.toString)),
        ("dependency_modules", Json.mkObj (deps.map fun d => (d.toString, originJson env d))),
        ("contract", Json.mkObj [("decl", Json.str n.toString),
          ("level_params", Json.arr (ci.levelParams.toArray.map nameJson)), ("type", exprJson ci.type),
          ("module", Json.str module), ("origins", origins)])]
    if let some contract := contract? then
      let name ← match (contract.getObjVal? "decl").bind Json.getStr? with
        | .ok name => pure name
        | .error e => throw (IO.userError e)
      if moduleOf env name.toName == some module.toName then
        let result ← checkContractInModule env targets contract
        let checked := match result with
          | .ok valid => Json.mkObj [("passed", Json.bool valid), ("module", Json.str module)]
          | .error e => Json.mkObj [("passed", Json.bool false), ("module", Json.str module), ("error", Json.str e)]
        checks := checks.push (module, checked)
  let mut check : Json := Json.null
  if let some contract := contract? then
    let selected := match (contract.getObjVal? "module").bind Json.getStr? with
      | .ok owner => checks.filter fun (entry : String × Json) => entry.1 == owner
      | .error _ => checks
    check := if selected.size == 1 then selected[0]!.2 else
      Json.mkObj [("passed", Json.bool false), ("error", Json.str
        (if selected.isEmpty then "candidate declaration missing from its owning module"
         else "candidate declaration is ambiguous across modules"))]
  IO.println (Json.mkObj [("declarations", Json.arr out),
    ("owned_names", Json.arr ownedNames), ("signature_check", check)]).compress
  return 0
