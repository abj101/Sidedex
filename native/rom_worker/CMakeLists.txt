cmake_minimum_required(VERSION 3.20)
project(sidedex_mgba_worker C)

include(FetchContent)
find_package(PkgConfig REQUIRED)
pkg_check_modules(libelf REQUIRED libelf)

# mGBA 0.10.5, pinned to the tag's commit so a moved tag cannot silently change
# the core. Keep dependency/build products under this directory's ignored build/.
set(LIBMGBA_ONLY ON CACHE BOOL "Build the mGBA core without frontends" FORCE)
set(BUILD_STATIC ON CACHE BOOL "Build libmgba as a static library" FORCE)
set(BUILD_SHARED OFF CACHE BOOL "Do not build a shared library" FORCE)
set(M_CORE_GBA ON CACHE BOOL "Build the Game Boy Advance core" FORCE)
set(M_CORE_GB OFF CACHE BOOL "Only build the Game Boy Advance core" FORCE)
set(USE_ELF ON CACHE BOOL "Allow loading pinned instrumented GBA ELF fixtures" FORCE)
set(CMAKE_POLICY_VERSION_MINIMUM 3.5 CACHE STRING "Compatibility for the pinned mGBA CMake project")

FetchContent_Declare(mgba
    GIT_REPOSITORY https://github.com/mgba-emu/mgba.git
    GIT_TAG 26b7884bc25a5933960f3cdcd98bac1ae14d42e2
    GIT_SHALLOW FALSE
)
FetchContent_MakeAvailable(mgba)

# The pinned mGBA feature probe misses MinGW's lowercase libelf pkg-config
# module, so wire the dependency explicitly and compile its ELF support.
target_compile_definitions(mgba PRIVATE USE_ELF)
target_include_directories(mgba PRIVATE ${libelf_INCLUDE_DIRS})
target_link_libraries(mgba ${libelf_LIBRARIES})

add_executable(rom_worker worker.c)
target_link_libraries(rom_worker PRIVATE mgba)
target_compile_definitions(rom_worker PRIVATE
    ROM_WORKER_VERSION="0.1.0"
    ROM_WORKER_MGBA_VERSION="0.10.5+26b7884"
)
