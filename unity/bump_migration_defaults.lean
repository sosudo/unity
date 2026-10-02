/- Native configured default target names. Does not build or update dependencies. -/
import Lake
import Lake.DSL
import Lake.Load.Package

open Lean Lake System

def main : IO UInt32 := do
  try
    let (elan?, lean?, lake?) ← findInstall?
    let some lean := lean? | throw <| IO.userError "missing Lean installation"
    let some lake := lake? | throw <| IO.userError "missing Lake installation"
    let lakeEnv ← match ← (Lake.Env.compute lake lean elan? (some true)).toBaseIO with
      | .ok env => pure env
      | .error message => throw <| IO.userError message
    let root ← IO.FS.realPath (← IO.currentDir)
    let some pkg ← (loadPackage { lakeEnv, wsDir := root, updateToolchain := false }).toBaseIO
      | throw <| IO.userError "could not load root Lake package"
    let mut targets : Array Json := #[]
    let mut issues : Array String := #[]
    for target in pkg.defaultTargets do
      if let some lib := pkg.findLeanLib? target then
        targets := targets.push <| Json.mkObj [
          ("name", Json.str target.toString), ("kind", Json.str "library"),
          ("path", Json.str (relPathFrom pkg.dir lib.srcDir).normalize.toString)]
      else if let some exe := pkg.findLeanExe? target then
        targets := targets.push <| Json.mkObj [
          ("name", Json.str target.toString), ("kind", Json.str "executable"),
          ("path", Json.str (relPathFrom pkg.dir exe.root.rootDir).normalize.toString)]
      else
        issues := issues.push s!"unsupported custom default target: {target}"
    IO.println <| (Json.mkObj [("targets", Json.arr targets), ("issues", toJson issues)]).compress
    return (if issues.isEmpty then 0 else 1 : UInt32)
  catch error =>
    IO.println <| (Json.mkObj [("targets", Json.arr #[]),
      ("issues", toJson [s!"Lake default target discovery failed: {error}"])]).compress
    return (1 : UInt32)
