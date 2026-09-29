"""Pinned ROM transition identity constants. Owned by the orchestrator; the
coordinated artifact flip edits only this file, pinned_source.json and the copies."""

PROTOCOL_VERSION = 3
WORKER_VERSION = "sidedex-mgba-worker/0.3.0"
MGBA_VERSION = "0.10.5+26b7884"
SAMPLER_VERSION = "paired-sfc32-v7-risk-first-crit-policy"
SAVE_FORMAT_ID = "mgba-savestate-savedata-rtc-v1"
STATE_SCHEMA = "sidedex-state-v1"
TRANSITION_SCHEMA = "sidedex-transition-v1"
SOURCE_SHA = "2576d0f3f4c861c760f0c55198e37e6d7b91683e"
# Gate 0 artifact: default source pin and build configuration, SHA-256.
ROM_SHA256 = "0CCAAAD0878A9CBFB36FBEFFF800E9B6DD66055924B7595038EDE1E8771EC5A2"
ENGINE_ELF_SHA256 = "2186323F7FB9F6279E6868F96DC76942EC8ECB1E425538052651DAAC20CA5CB7"
WORKER_SHA256 = "DD6F1DD646D1B1AABCCA9EDB7F3C5CFF3100A90FB8AAF0DAA538264A953AAE60"
# Native i386 engine (WSL /home/abj1/rom-n1/sidedex-native, worker sidedex-native/0.1.0), SHA-256; cache key native-v1.
NATIVE_ENGINE_SHA256 = "628605865E696CA5B3672DC1AB6487D60D7EC9A9BAE23CBC14A6E0AF0887F9A4"
NATIVE_WORKER_VERSION, NATIVE_CORE = "sidedex-native/0.1.0", "native-i386"  # status `worker` / `mgba`
# The native engine claims byte equality with ENGINE_ELF_SHA256 (Gate N2); its status reports its own binary as the ROM.

CANDIDATE_WORKER_V3_SHA256 = "DD6F1DD646D1B1AABCCA9EDB7F3C5CFF3100A90FB8AAF0DAA538264A953AAE60"
