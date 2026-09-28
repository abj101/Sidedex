param(
    # Dev/variant builds (B1a experiments, LTO) go to their own directory so
    # build/mingw can hold the pinned worker the release-pin tests read.
    [string]$BuildDir = 'mingw',
    [string]$CMakeFlags = ''
)

$ErrorActionPreference = 'Stop'

$bash = 'C:\msys64\usr\bin\bash.exe'
$gcc = 'C:\msys64\mingw64\bin\x86_64-w64-mingw32-gcc.exe'
if (!(Test-Path -LiteralPath $bash) -or !(Test-Path -LiteralPath $gcc)) {
    throw 'Install MSYS2 and packages mingw-w64-x86_64-gcc, mingw-w64-x86_64-cmake, and mingw-w64-x86_64-ninja first.'
}

$env:MSYSTEM = 'MINGW64'
$env:Path = 'C:\msys64\mingw64\bin;C:\msys64\usr\bin;' + $env:Path
$projectPath = (& 'C:\msys64\usr\bin\cygpath.exe' -u $PSScriptRoot).Trim()
$buildPath = (& 'C:\msys64\usr\bin\cygpath.exe' -u ($PSScriptRoot + '\build\' + $BuildDir)).Trim()
$bashCommand = "cmake -S '$projectPath' -B '$buildPath' -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=x86_64-w64-mingw32-gcc -DCMAKE_CXX_COMPILER=x86_64-w64-mingw32-g++ $CMakeFlags && cmake --build '$buildPath' --config Release"
& $bash -lc $bashCommand
if ($LASTEXITCODE -ne 0) {
    throw "mGBA worker build failed with exit code $LASTEXITCODE"
}
