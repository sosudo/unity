/-
Optimize-only Lake module discovery. Compile once per helper/toolchain revision:

  lake env lean -R <this file's directory> -c <cache>/workspace.c <this file>
  lake env leanc -o <cache>/workspace <cache>/workspace.c -lLake -rdynamic

Then run from the package root:

  lake env <cache>/workspace src/Example.lean src/Example/Defs.lean Main.lean

Native linking and interpreter symbol export (`-rdynamic` on Unix) are required
to evaluate arbitrary lakefile.lean configuration. The cached-configuration Lake
loader cannot safely run through `lean --run`; TOML alone does not expose this.

The final stdout line is JSON with `modules` (project-relative source path to
import name), `traces` (module name to package-relative trace file), `build_dir`,
`source_roots`, `unmatched`, and `issues`, plus `libraries` (Lake target names)
and `module_owners` (source path to library/executable target name arrays).
Pass the controller's
already checked source paths; unowned scratch files are reported as unmatched.
The caller must reject an unmatched required target, and must still check source
containment/symlinks. Pass --layout-only to inspect configuration without source
enumeration. With no arguments, Lake's configured library globs and
executable roots are enumerated (not the transitive imports of those modules).

Use Lake's actual Lean/TOML package loader and source lookup, not a TOML parser
or path-to-namespace heuristic. Root-only loading deliberately avoids dependency
updates/materialization: loadWorkspace invokes the same package loader but may
update the manifest when it is missing. No build is performed by this helper.
-/
import Lake
import Lake.DSL
import Lake.Load.Package

open Lean Lake System

namespace UnityOptimizeWorkspace

-- These adapters use the Lake APIs shared by Lean 4.19 and later. Optimize
-- keeps the project's toolchain; it cannot depend on migration-only APIs.
private def normalizedPath (path : FilePath) : FilePath :=
  -- Older FilePath.normalize does not remove Lake's default `/./` segments.
  -- Do not resolve `..` or symlinks here; checkedSource still compares realpath.
  let parts := path.normalize.components.filter (· != ".")
  if parts.isEmpty then "." else mkFilePath parts

private def dropComponents? : List String → List String → Option (List String)
  | [], rest => some rest
  | root :: roots, path :: paths =>
    if root == path then dropComponents? roots paths else none
  | _, _ => none

private def relativePath? (root path : FilePath) : Option FilePath := do
  let parts ← dropComponents? (normalizedPath root).components (normalizedPath path).components
  pure <| if parts.isEmpty then "." else mkFilePath parts

private def relativeOrAbsolute (root path : FilePath) : FilePath :=
  (relativePath? root path).getD (normalizedPath path)

private def libraryModuleBySource? (lib : LeanLib) (path : FilePath) : Option Lake.Module := do
  let relative ← relativePath? lib.srcDir path
  if relative.extension != some "lean" then none else do
    let name := (relative.withExtension "").components.foldl Name.str Name.anonymous
    -- Source-root-relative naming is only a candidate. Lake's configured
    -- roots/globs must admit it, and its resulting module must name this file.
    let mod ← lib.findModule? name
    if normalizedPath mod.leanFile == normalizedPath path then some mod else none

private def checkedSource (root : FilePath) (filename : String) : IO FilePath := do
  let relative : FilePath := filename
  if relative.isAbsolute || relative.components.contains ".." || relative.extension != some "lean" then
    throw <| IO.userError s!"invalid project source path: {filename}"
  let path := normalizedPath (root / relative)
  if (← IO.FS.realPath path) != path then
    throw <| IO.userError s!"project source is not canonical (symlink or alias): {filename}"
  return path

private def loadRoot (lakeOpts : NameMap String := {}) : IO Package := do
  let (elan?, lean?, lake?) ← findInstall?
  let some lean := lean? | throw <| IO.userError "could not locate Lean installation"
  let some lake := lake? | throw <| IO.userError "could not locate Lake installation"
  let lakeEnv ← match ← (Lake.Env.compute lake lean elan? (some true)).toBaseIO with
    | .ok env => pure env
    | .error message => throw <| IO.userError message
  let root ← IO.FS.realPath (← IO.currentDir)
  let some pkg ← (loadPackage {
    lakeEnv, lakeOpts, wsDir := root, updateToolchain := false, reconfigure := true
  }).toBaseIO
    | throw <| IO.userError "could not load the root Lake package configuration"
  return pkg

private def discover (paths : List String) (lakeOpts : NameMap String := {}) : IO Json := do
  let pkg ← loadRoot lakeOpts
  let layoutOnly := paths == ["--layout-only"]
  let mut paths := if layoutOnly then [] else paths
  let mut roots : Array Json := #[]
  let mut defaultModules : List (String × Json) := []
  let mut unknownDefaults : Array String := #[]
  for target in pkg.defaultTargets do
    if let some lib := pkg.findLeanLib? target then
      for mod in ← lib.getModuleArray do
        defaultModules := ((relativeOrAbsolute pkg.dir mod.leanFile).toString, Json.str mod.name.toString) :: defaultModules
    else if let some exe := pkg.findLeanExe? target then
      defaultModules := ((relativeOrAbsolute pkg.dir exe.root.leanFile).toString, Json.str exe.root.name.toString) :: defaultModules
    else
      unknownDefaults := unknownDefaults.push target.toString
  for lib in pkg.leanLibs do
    roots := roots.push <| Json.mkObj [
      ("kind", Json.str "library"), ("name", Json.str lib.name.toString),
      ("path", Json.str (relativeOrAbsolute pkg.dir lib.srcDir).toString)]
  for exe in pkg.leanExes do
    roots := roots.push <| Json.mkObj [
      ("kind", Json.str "executable"), ("name", Json.str exe.name.toString),
      ("path", Json.str (relativeOrAbsolute pkg.dir exe.root.rootDir).toString)]
  if paths.isEmpty && !layoutOnly then
    for lib in pkg.leanLibs do
      for mod in ← lib.getModuleArray do
        paths := paths ++ [(relativeOrAbsolute pkg.dir mod.leanFile).toString]
    for exe in pkg.leanExes do
      paths := paths ++ [(relativeOrAbsolute pkg.dir exe.root.leanFile).toString]
  let mut modules : List (String × Json) := []
  let mut owners : List (String × Json) := []
  let mut traces : List (String × Json) := []
  let mut unmatched : Array String := #[]
  let mut issues : Array String := #[]
  let mut seen : NameMap String := {}
  for filename in paths.eraseDups do
    let relative : FilePath := filename
    if relative.isAbsolute || relative.components.contains ".." || relative.extension != some "lean" then
      issues := issues.push s!"invalid project source path: {filename}"
      continue
    let path ← checkedSource pkg.dir filename
    let mut candidates : Array Lake.Module := #[]
    let mut libraryOwners : Array String := #[]
    let mut executableOwners : Array String := #[]
    for lib in pkg.leanLibs do
      if let some mod := libraryModuleBySource? lib path then
        candidates := candidates.push mod
        libraryOwners := libraryOwners.push lib.name.toString
    for exe in pkg.leanExes do
      if normalizedPath exe.root.leanFile == path then
        candidates := candidates.push exe.root
        executableOwners := executableOwners.push exe.name.toString
    let some mod := candidates[0]? | do
      unmatched := unmatched.push filename
      continue
    if candidates.any (·.name != mod.name) then
      issues := issues.push s!"ambiguous module ownership for source {filename}"
      continue
    -- Prefix-based source lookup alone can accept similarly named directories.
    -- Also require the canonical module target to resolve to these exact bytes.
    let some canonical := pkg.findTargetModule? mod.name | do
      issues := issues.push s!"source {filename} has no buildable module target"
      continue
    if normalizedPath mod.leanFile != path || normalizedPath canonical.leanFile != path then
      issues := issues.push s!"source {filename} conflicts with module {mod.name}"
      continue
    -- Lake resolves overlapping library declarations by their order. That must
    -- not hide another owner of the same module name at a different path.
    let conflictingLib := pkg.leanLibs.any fun lib =>
      (lib.findModule? mod.name).any (fun candidate => normalizedPath candidate.leanFile != path)
    let conflictingExe := pkg.leanExes.any fun exe =>
      (exe.isRoot? mod.name).any (fun candidate => normalizedPath candidate.leanFile != path)
    if conflictingLib || conflictingExe then
      issues := issues.push s!"ambiguous canonical ownership for module {mod.name} at {filename}"
      continue
    if let some previous := seen.find? mod.name then
      if previous != relative.normalize.toString then
        issues := issues.push s!"module {mod.name} maps to both {previous} and {filename}"
        continue
    seen := seen.insert mod.name relative.normalize.toString
    modules := (relative.normalize.toString, Json.str mod.name.toString) :: modules
    owners := (relative.normalize.toString, Json.mkObj [
      ("libraries", toJson (libraryOwners.qsort (· < ·))),
      ("executables", toJson (executableOwners.qsort (· < ·)))]) :: owners
    traces := (mod.name.toString,
      Json.str (relativeOrAbsolute pkg.dir mod.traceFile).toString) :: traces
  return Json.mkObj [
    ("modules", Json.mkObj modules), ("source_roots", Json.arr roots),
    ("module_owners", Json.mkObj owners),
    ("default_modules", Json.mkObj defaultModules),
    ("unknown_default_targets", toJson unknownDefaults),
    ("libraries", toJson ((pkg.leanLibs.map (·.name.toString)).qsort (· < ·))),
    ("traces", Json.mkObj traces),
    ("build_dir", Json.str (relativeOrAbsolute pkg.dir pkg.buildDir).toString),
    ("unmatched", toJson unmatched), ("issues", toJson issues)]

end UnityOptimizeWorkspace

private def readLakeOptions : List String → NameMap String → IO (NameMap String × List String)
  | "--lake-option" :: key :: value :: rest, options =>
      if key.isEmpty then throw <| IO.userError "empty Lake configuration key"
      else readLakeOptions rest (options.insert key.toName value)
  | "--lake-option" :: _, _ => throw <| IO.userError "incomplete Lake configuration option"
  | rest, options => pure (options, rest)

def main (args : List String) : IO UInt32 := do
  try
    let (options, args) ← readLakeOptions args {}
    let result ← UnityOptimizeWorkspace.discover args options
    IO.println result.compress
    return if (result.getObjValAs? (Array String) "issues").toOption == some #[] then (0 : UInt32) else 1
  catch error =>
    IO.println (Json.mkObj [("modules", Json.mkObj []), ("source_roots", Json.arr #[]),
      ("module_owners", Json.mkObj []), ("libraries", Json.arr #[]),
      ("unmatched", Json.arr #[]), ("issues", toJson [s!"Lake workspace discovery failed: {error}"])]).compress
    return (1 : UInt32)
