/- Unity's exact blueprint extractor (the LeanArchitect mechanism, without requiring the
   project to depend on LeanArchitect or annotate declarations).

   Loads the project's compiled modules and dumps every project-owned declaration with its
   kind, direct sorry usage, and used-constant dependencies, as one JSON array on stdout.

   Run from the project root (needs `lake build` to have produced the .oleans):
       lake env lean --run <this file> Module.One Module.Two ...
   Unbuilt/broken projects fail fast — the caller falls back to textual parsing. -/
import Lean
open Lean

def kindOf : ConstantInfo → String
  | .thmInfo _    => "theorem"
  | .defnInfo _   => "def"
  | .axiomInfo _  => "axiom"
  | .opaqueInfo _ => "opaque"
  | .inductInfo _ => "inductive"
  | .ctorInfo _   => "constructor"
  | .recInfo _    => "recursor"
  | .quotInfo _   => "quot"

/-- Compiler-generated auxiliaries that would drown the blueprint. -/
def isNoise (n : Name) : Bool :=
  n.isInternal || n.components.any fun c =>
    let s := c.toString
    s.startsWith "match_" || s.startsWith "proof_" || s.startsWith "eq_" ||
    s.startsWith "injEq" || s.startsWith "sizeOf" || s.startsWith "_"

def main (args : List String) : IO UInt32 := do
  initSearchPath (← findSysroot)
  -- importAll loads the private olean part too (proof terms — needed for sorry/dep
  -- detection under the Lean ≥4.20 module system). On much older toolchains the field
  -- doesn't exist and this script fails to compile; the caller falls back to regex.
  let mods := args.toArray.map fun a => ({ module := a.toName, importAll := true } : Import)
  let env ← importModules mods {} 0
  let targets : NameSet := args.foldl (fun s a => s.insert a.toName) {}
  let moduleOf (n : Name) : Option Name := do
    let idx ← env.getModuleIdxFor? n
    env.header.moduleNames[idx.toNat]?
  let isTarget (n : Name) : Bool := (moduleOf n).map targets.contains |>.getD false
  let mut out : Array Json := #[]
  for (n, ci) in env.constants.toList do
    unless isTarget n && !isNoise n do continue
    let used := ci.type.getUsedConstants ++
      (((ci.value? (allowOpaque := true)).map (·.getUsedConstants)).getD #[])
    let sorried := used.contains ``sorryAx
    let deps := (used.filter fun d => isTarget d && d != n && !isNoise d).toList.eraseDups
    out := out.push <| Json.mkObj [
      ("name", Json.str n.toString),
      ("kind", Json.str (kindOf ci)),
      ("module", Json.str (((moduleOf n).getD .anonymous).toString)),
      ("sorried", Json.bool sorried),
      ("deps", Json.arr (deps.toArray.map fun d => Json.str d.toString))]
  IO.println (Json.arr out).compress
  return 0
