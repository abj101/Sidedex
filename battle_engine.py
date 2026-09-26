"""ROM-faithful, standalone battle core for the Sidedex Calc cards.

Pure Python, no IO, no streamlit, no clock reads other than the caller-supplied
``deadline``.  Every arithmetic path is transcribed from the pinned
pokeemerald-expansion checkout (``SOURCE_SHA``); source citations are
``src/...:line`` / ``include/...:line`` comments next to the code they justify.

Supported-mechanic registry (declared, narrow, fail-closed)
----------------------------------------------------------
The engine refuses to invent numbers.  A state is only "supported" when every
input it needs is present and the mechanic is actually ported:

* Singles, one player active vs one enemy active (1v1 on the field).
* Damaging moves with ``category`` physical/special, ``effect`` of
  ``EFFECT_HIT`` or ``EFFECT_MULTI_HIT``, ``power`` > 0, integer ``accuracy``
  of 0 (never miss) or 1..100, integer ``priority``, ``always_critical`` false,
  integer ``crit_ratio`` 0..4, integer ``strike_count`` >= 1, an empty
  ``secondary_effects`` list, ``recoil`` == 0 and only move flags listed in
  ``INERT_MOVE_FLAGS``.
* Stat calculation (level/IV/EV/nature), damage with the 85..100 roll, critical
  hits, STAB, the type chart, the accuracy check, multi-hit counts,
  priority/action order with the ROM speed-tie table, and the SFC32 RNG with an
  entered 4-word state.
* Trainer AI action selection for ``ai_flags == []`` and
  ``ai_flags == ["AI_FLAG_CHECK_BAD_MOVE"]`` (``src/battle_ai_main.c:492``,
  ``:724``).  Only the scoring rules of ``AI_CheckBadMove`` that are reachable
  inside this slice are ported (the type-immunity penalty, ``:778``); any other
  flag stays ``unsupported-mechanic``.
* Trainer bag items: HP-restore, full restore and revive classes only, using a
  transcribed item table.  Anything else is ``unsupported-item``.
* Easy difficulty only through the resolvable boss-level adjustment
  (``src/battle_main.c:2075`` / ``:2087``).

Deliberately *not* ported, and therefore rejected with a reason code: held
items (``use_item`` false, or the ``ITEM_NONE`` sentinel), statuses, stat
stages, weather/terrain/trick room/screens/hazards, secondary effects, recoil,
unlisted move flags and AI switching.  ``None`` in any of those slots means
*unknown* and is reported as ``missing-input`` with the dotted path.  The one
exception is ``item``: with ``use_item`` false a null ``item`` simply means "no
held item" (contract section 2, BUILD); with ``use_item`` true a null ``item``
is still ``missing-input``.

Gating is per card and per entry (contract section 3)
----------------------------------------------------
A card is never suppressed by an input its own computation does not read.  Each
card resolves only the party members it walks: ``player_moves``/``enemy_moves``
need the two actives (plus ``planned_action`` for ``enemy_moves``),
``ai_actions`` needs the enemy active's legal actions plus the player active,
and ``recommendation`` is strict -- every legal first-turn player action is a
reachable path, so a switch candidate or move it cannot evaluate makes the whole
card ``unavailable`` (``incomplete-search``, naming the action).  Within
``player_moves``/``enemy_moves`` each move is gated on its own: an unported move
carries ``status: unavailable`` with its own reason/mechanics while its supported
siblings stay exact, and the card is exact when at least one entry is exact.
``no-legal-actions`` is reserved for a moveset with nothing selectable at all
(every slot ``MOVE_NONE`` or at 0 PP).  A *legal* move that simply cannot damage
the target -- a type immunity, the case ``SwitchRecommendationIntegration``
exercises -- is still an exact entry with ``probability 0/1`` and an immunity
note, because the ROM can select it (``IsMoveUnusable``
``include/battle_ai_util.h:60`` has no effectiveness term; the AI keeps such a
move at ``AI_SCORE_DEFAULT - 20 = 80 > 0``, ``src/battle_ai_main.c:779``, and
``IsDamageMoveUnusable`` ``src/battle_ai_util.c:446`` only suppresses its *damage
estimate* inside ``AI_CalcDamage``).

The enemy bench is only reached through the switch decision.  ``ShouldSwitch``
(``src/battle_ai_switch_items.c:1018``) is provably FALSE whenever the active has
a usable damaging move (``ShouldSwitchIfAllMovesBad`` ``:388``) and no valid bench
member knows Baton Pass (``GetBestMonBatonPass`` ``:1323`` returns ``PARTY_SIZE``
without drawing RNG), so in that case no bench input is required at all; when the
switch *is* reachable the needed bench inputs are named in ``missing`` and the
unported check is reported in ``mechanics``.

Abilities are supported only from ``INERT_ABILITIES``: a curated list where
source inspection proves the ability cannot change damage, accuracy, priority,
type effectiveness/immunity or either active's HP *inside this slice*.  Every
entry carries the ``src/path.c:LINE`` that justifies it.  Anything else -- and
the unknown-ability ``None`` case (``missing-input``) -- is
``unsupported-ability``.

``rng.state`` convention
------------------------
``snapshot["rng"]["state"]`` is the live 4-word SFC32 state **at the start of
the turn**: after the previous turn resolved and before that turn's speed-tie
draw.  ``evaluate_head_to_head`` then consumes RNG in ROM order -- speed-tie
draw (``src/battle_main.c:4161``), AI action selection (``src/battle_ai_main.c:492``),
the per-battler Quick Draw rolls (``src/battle_main.c:5268``), then each
action's own draws.

Probability rule (contract section 3)
-------------------------------------
An exact ``probability`` is only ever ``known-seed`` (simulated from
``rng.state``) or ``seed-independent`` (provably identical for every RNG
state).  Anything that would need enumeration over unknown seeds returns
``unavailable`` with ``reason: "unknown-rng-seed"``.  No sampling, no
independent-draw substitution, no float accumulation: probabilities and scores
are exact integers / ``fractions.Fraction``.
"""

from fractions import Fraction

ENGINE_VERSION = "1.3.3"

# data/source_lock.json -> commit (pinned pokeemerald-expansion checkout).
SOURCE_SHA = "2576d0f3f4c861c760f0c55198e37e6d7b91683e"

REASONS = frozenset((
    "missing-input", "unknown-rng-seed", "unrepresentable-history",
    "unsupported-mechanic", "unsupported-move", "unsupported-ability",
    "unsupported-item", "unsupported-difficulty", "conditional-party",
    "incomplete-search", "timeout", "no-legal-actions", "source-mismatch",
    "engine-error",
))

EVIDENCE_BASE = {"basis": "source-transcribed", "oracle_verified": False}

MAX_LEVEL = 100                 # include/constants/pokemon.h:148
UQ_4_12_ONE = 4096              # include/fpmath.h:12

STATS = ("hp", "attack", "defense", "sp_attack", "sp_defense", "speed")

# src/pokemon.c:378 gNaturesInfo -> statUp/statDown per nature id.
NATURES = {
    "NATURE_HARDY": ("attack", "attack"), "NATURE_LONELY": ("attack", "defense"),
    "NATURE_BRAVE": ("attack", "speed"), "NATURE_ADAMANT": ("attack", "sp_attack"),
    "NATURE_NAUGHTY": ("attack", "sp_defense"), "NATURE_BOLD": ("defense", "attack"),
    "NATURE_DOCILE": ("defense", "defense"), "NATURE_RELAXED": ("defense", "speed"),
    "NATURE_IMPISH": ("defense", "sp_attack"), "NATURE_LAX": ("defense", "sp_defense"),
    "NATURE_TIMID": ("speed", "attack"), "NATURE_HASTY": ("speed", "defense"),
    "NATURE_SERIOUS": ("speed", "speed"), "NATURE_JOLLY": ("speed", "sp_attack"),
    "NATURE_NAIVE": ("speed", "sp_defense"), "NATURE_MODEST": ("sp_attack", "attack"),
    "NATURE_MILD": ("sp_attack", "defense"), "NATURE_QUIET": ("sp_attack", "speed"),
    "NATURE_BASHFUL": ("sp_attack", "sp_attack"), "NATURE_RASH": ("sp_attack", "sp_defense"),
    "NATURE_CALM": ("sp_defense", "attack"), "NATURE_GENTLE": ("sp_defense", "defense"),
    "NATURE_SASSY": ("sp_defense", "speed"), "NATURE_CAREFUL": ("sp_defense", "sp_attack"),
    "NATURE_QUIRKY": ("sp_defense", "sp_defense"),
}

# src/battle_script_commands.c:2026 sGen7CriticalHitOdds (B_CRIT_CHANCE >= GEN_7)
CRIT_ODDS = (24, 8, 2, 1, 1)
CRIT_MULTIPLIER = 6144          # UQ_4_12(1.5)  include/config/battle.h:6
STAB_MULTIPLIER = 6144          # UQ_4_12(1.5)  src/battle_util.c:9975
DMG_ROLL_LO, DMG_ROLL_HI = 85, 100                    # include/battle_util.h:89
DMG_ROLL_DEFAULT_PERCENT = 93                          # src/battle_ai_util.h:9

SUPPORTED_EFFECTS = ("EFFECT_HIT", "EFFECT_MULTI_HIT")

# Only these AI flags are ported.  AI_CheckBadMove is the sole scoring function
# of the CHECK_BAD_MOVE flag (sBattleAiFuncTable, src/battle_ai_main.c:138) and
# so is the only one that runs when the flag set is exactly this one.  Any other
# flag adds a scoring function whose rules are not ported -> unsupported-mechanic.
SUPPORTED_AI_FLAGS = ("AI_FLAG_CHECK_BAD_MOVE",)

AI_SCORE_DEFAULT = 100          # include/constants/battle_ai.h:55

# Abilities that provably cannot change damage, accuracy, priority, type
# effectiveness/immunity or either active's HP inside the supported slice.
# Each value is (source citation, why it cannot matter here).  Anything not
# listed stays unsupported-ability; see the module docstring.
INERT_ABILITIES = {
    # -- their trigger cannot occur in this slice --
    "ABILITY_ROCK_HEAD": (
        "data/battle_scripts_1.s:7095",
        "prevents recoil, but recoil moves and Struggle are outside the supported move set"),
    "ABILITY_PICKUP": (
        "data/battle_scripts_1.s:7261",
        "recycles a consumed item, but supported party members cannot hold items"),
    # -- never consulted by the pinned source at all --
    "ABILITY_STEADFAST": (
        "src/ (no occurrence)",
        "no reference to this id exists anywhere in the pinned source tree"),
    # -- switch-in message only: no state change, no RNG draw --
    "ABILITY_FRISK": (
        "src/battle_util.c:4563",
        "switch-in message only, so it cannot alter damage/accuracy/order/HP"),
    "ABILITY_ANTICIPATION": (
        "src/battle_util.c:4533",
        "switch-in message only; its CalcTypeEffectivenessMultiplier call uses updateFlags FALSE"),
    "ABILITY_RUN_AWAY": (
        "src/battle_main.c:4278",
        "only read by the flee check, and a trainer battle cannot flee"),
    "ABILITY_BALL_FETCH": (
        "src/battle_util.c:5230",
        "requires a thrown ball (gBattleResults.catchAttempts >= 1), impossible in a trainer battle"),
    # -- status application only: the slice forbids status and has no status source --
    "ABILITY_INSOMNIA": ("src/battle_util.c:6213", "only blocks sleep, which the slice cannot inflict or hold"),
    "ABILITY_VITAL_SPIRIT": ("src/battle_util.c:6214", "only blocks sleep, which the slice cannot inflict or hold"),
    "ABILITY_IMMUNITY": ("src/battle_util.c:6191", "only blocks poison, which the slice cannot inflict or hold"),
    "ABILITY_LIMBER": ("src/battle_util.c:6206", "only blocks paralysis, which the slice cannot inflict or hold"),
    "ABILITY_WATER_VEIL": ("src/battle_util.c:6223", "only blocks burn, which the slice cannot inflict or hold"),
    "ABILITY_MAGMA_ARMOR": ("src/battle_util.c:6231", "only blocks freeze, which the slice cannot inflict or hold"),
    "ABILITY_OWN_TEMPO": ("src/battle_util.c:6199", "only blocks confusion, which the slice cannot inflict or hold"),
    "ABILITY_OBLIVIOUS": ("src/battle_util.c:6238", "only blocks infatuation/Taunt, which the slice cannot inflict"),
    "ABILITY_COMATOSE": ("src/battle_util.c:4655", "is itself only a permanent sleep status; status must be none"),
    "ABILITY_PASTEL_VEIL": ("src/battle_util.c:4523", "only cures/blocks poison, which the slice cannot inflict"),
    "ABILITY_SWEET_VEIL": ("src/battle_util.c:6651", "only blocks sleep, which the slice cannot inflict or hold"),
    "ABILITY_HEALER": ("src/battle_util.c:5206", "end-of-turn ally status cure; no status and no ally in singles"),
    "ABILITY_SHED_SKIN": ("src/battle_util.c:5099", "end-of-turn status cure; the slice has no status"),
    "ABILITY_HYDRATION": ("src/battle_util.c:5092", "end-of-turn status cure under rain, which the slice excludes"),
    "ABILITY_EARLY_BIRD": ("src/battle_util.c:3183", "halves the sleep counter; the slice has no sleep"),
    "ABILITY_NATURAL_CURE": ("src/battle_ai_main.c:3452", "cures status on switch-out; no status and no enemy switch"),
    "ABILITY_INNER_FOCUS": ("src/battle_script_commands.c:3410", "only blocks flinching, and the slice has no flinch source"),
    "ABILITY_SHIELD_DUST": ("src/battle_script_commands.c:3055", "only suppresses additional move effects, and no move here has any"),
    # -- status-conditional boosts: guarded by a status bit the slice forbids --
    "ABILITY_GUTS": ("src/battle_util.c:9593", "x1.5 Attack only while status1 & STATUS1_ANY, impossible here"),
    "ABILITY_MARVEL_SCALE": ("src/battle_util.c:9796", "x1.5 Defense only while status1 & STATUS1_ANY, impossible here"),
    "ABILITY_QUICK_FEET": ("src/battle_main.c:4937", "x1.5 Speed only while status1 & STATUS1_ANY, impossible here"),
    "ABILITY_TOXIC_BOOST": ("src/battle_util.c:9165", "x1.5 power only while poisoned, impossible here"),
    "ABILITY_FLARE_BOOST": ("src/battle_util.c:9161", "x1.5 power only while burned, impossible here"),
    "ABILITY_POISON_HEAL": ("src/battle_util.c:2372", "only heals poison, which the slice cannot inflict or hold"),
    # -- weather/terrain gated: the slice requires field.weather == None --
    "ABILITY_CHLOROPHYLL": ("src/battle_main.c:4928", "x2 Speed only in sun, excluded by field.weather"),
    "ABILITY_SWIFT_SWIM": ("src/battle_main.c:4926", "x2 Speed only in rain, excluded by field.weather"),
    "ABILITY_SLUSH_RUSH": ("src/battle_main.c:4932", "x2 Speed only in hail/snow, excluded by field.weather"),
    "ABILITY_SAND_RUSH": ("src/battle_main.c:4930", "x2 Speed only in sandstorm, excluded by field.weather"),
    "ABILITY_SAND_VEIL": ("src/battle_script_commands.c:1692", "accuracy drop only in sandstorm, excluded by field.weather"),
    "ABILITY_SNOW_CLOAK": ("src/battle_script_commands.c:1696", "accuracy drop only in hail/snow, excluded by field.weather"),
    "ABILITY_LEAF_GUARD": ("src/battle_script_commands.c:9155", "status immunity only in sun, excluded by field.weather"),
    "ABILITY_RAIN_DISH": ("src/battle_util.c:5079", "end-of-turn heal only in rain, excluded by field.weather"),
    "ABILITY_ICE_BODY": ("src/battle_util.c:5819", "end-of-turn heal only in hail/snow, excluded by field.weather"),
    "ABILITY_FORECAST": ("src/battle_util.c:6372", "form/type change only under weather, excluded by field.weather"),
    "ABILITY_SOLAR_POWER": ("src/battle_util.c:9535", "x1.5 SpAtk only in sun, excluded by field.weather"),
    "ABILITY_FLOWER_GIFT": ("src/battle_util.c:9581", "x1.5 Attack only in sun, excluded by field.weather"),
    "ABILITY_CLOUD_NINE": ("src/battle_util.c:4780", "only suppresses weather; there is no weather to suppress"),
    "ABILITY_AIR_LOCK": ("src/battle_util.c:4781", "only suppresses weather; there is no weather to suppress"),
    # -- held-item gated: the slice has no held items --
    "ABILITY_UNBURDEN": ("src/battle_script_commands.c:2944", "needs a held item to be consumed, which the slice forbids"),
    "ABILITY_GLUTTONY": ("src/battle_util.c:6759", "only changes when a held Berry is eaten, no items here"),
    "ABILITY_CHEEK_POUCH": ("src/battle_script_commands.c:8447", "only triggers on eating a held Berry, no items here"),
    "ABILITY_PICKPOCKET": ("src/battle_script_commands.c:6718", "steals a held item, no items here"),
    "ABILITY_STICKY_HOLD": ("src/battle_script_commands.c:5692", "only prevents losing a held item, no items here"),
    "ABILITY_KLUTZ": ("src/battle_util.c:8459", "only suppresses its own held item, no items here"),
    "ABILITY_RIPEN": ("src/battle_util.c:6781", "only doubles Berry effects, no items here"),
    "ABILITY_HARVEST": ("src/battle_util.c:5064", "recycles a held Berry, no items here"),
    "ABILITY_CUD_CHEW": ("src/battle_util.c:5252", "repeats a eaten Berry, no items here"),
    # -- singles only: every reference needs a battle partner (doubles) --
    "ABILITY_PLUS": ("src/battle_util.c:9563", "needs a living partner with Plus/Minus; singles 1v1"),
    "ABILITY_MINUS": ("src/battle_util.c:9572", "needs a living partner with Plus/Minus; singles 1v1"),
    "ABILITY_TELEPATHY": ("src/battle_util.c:10600", "only exempts BATTLE_PARTNER(battlerAtk); singles 1v1"),
    "ABILITY_BATTERY": ("src/battle_util.c:9288", "only boosts a living partner's special moves; singles 1v1"),
    "ABILITY_POWER_SPOT": ("src/battle_util.c:9292", "only boosts a living partner's moves; singles 1v1"),
    "ABILITY_FLOWER_VEIL": ("src/battle_script_commands.c:9150", "IsAbilityOnSide guard for a partner; singles 1v1"),
    # -- stat-stage only: the slice requires every stat stage at its default --
    "ABILITY_UNAWARE": ("src/battle_util.c:9505", "only resets stat stages, which are already at default"),
    "ABILITY_KEEN_EYE": ("src/battle_script_commands.c:1633", "only resets accuracy/evasion stages, already at default"),
    "ABILITY_HYPER_CUTTER": ("src/battle_script_commands.c:5621", "only blocks Attack drops, and no stat can change"),
    "ABILITY_BIG_PECKS": ("src/battle_script_commands.c:5622", "only blocks Defense drops, and no stat can change"),
    "ABILITY_CLEAR_BODY": ("src/battle_script_commands.c:16293", "only blocks stat drops, and no stat can change"),
    "ABILITY_WHITE_SMOKE": ("src/battle_script_commands.c:16295", "only blocks stat drops, and no stat can change"),
    "ABILITY_FULL_METAL_BODY": ("src/battle_script_commands.c:16294", "only blocks stat drops, and no stat can change"),
    "ABILITY_CONTRARY": ("src/battle_script_commands.c:5585", "only inverts stat changes, and no stat can change"),
    "ABILITY_DEFIANT": ("src/battle_util.c:1004", "only reacts to its own stat drops, and no stat can change"),
    "ABILITY_COMPETITIVE": ("src/battle_util.c:1005", "only reacts to its own stat drops, and no stat can change"),
}
# Move flags that provably cannot change damage, accuracy, priority, type
# effectiveness/immunity or either active's HP inside the slice.  Each value is
# (source citation, why it cannot matter here).  Any flag not listed -- including
# one a future extractor adds -- rejects the move (``flag:<name>``).
INERT_MOVE_FLAGS = {
    # -- read only by abilities/items the slice rejects --
    "makesContact": (
        "src/battle_util.c:8487",
        "IsMoveMakingContact feeds ability/item contact reactions (e.g. Tough Claws :9200); all such abilities are non-inert and no held item exists"),
    "punchingMove": (
        "src/battle_util.c:9174",
        "only read by ABILITY_IRON_FIST and the Punching Glove item (:8498)"),
    "kickingMove": (
        "src/battle_util.c:9178",
        "only read by ABILITY_STRIKER and the Kicking Shoes item (:8501)"),
    "bitingMove": (
        "src/battle_util.c:9205",
        "only read by ABILITY_STRONG_JAW"),
    "pulseMove": (
        "src/battle_util.c:9209",
        "only read by ABILITY_MEGA_LAUNCHER"),
    "slicingMove": (
        "src/battle_util.c:9257",
        "only read by ABILITY_SHARPNESS"),
    "boneMove": (
        "src/battle_util.c:10075",
        "only read by ABILITY_BONE_ZONE"),
    "ballisticMove": (
        "src/battle_util.c:4042",
        "only read by ABILITY_BULLETPROOF's block check"),
    "soundMove": (
        "src/battle_util.c:4038",
        "only read by ABILITY_SOUNDPROOF's block check"),
    "windMove": (
        "src/battle_util.c:4137",
        "only read by ABILITY_WIND_RIDER"),
    "danceMove": (
        "src/battle_util.c:6135",
        "only read by ABILITY_DANCER, which needs a partner that used the dance"),
    "ignoresTargetAbility": (
        "src/battle_ai_util.c:458",
        "only suppresses a defender ability, and every accepted ability is inert"),
    # -- read only against state the slice forbids --
    "ignoresTargetDefenseEvasionStages": (
        "src/battle_util.c:9784",
        "sets the target's defense stage to DEFAULT, which the slice already requires"),
    "minimizeDoubleDamage": (
        "src/battle_util.c:10005",
        "guarded by STATUS3_MINIMIZED, which the slice cannot hold"),
    "damagesAirborneDoubleDamage": (
        "src/battle_util.c:10026",
        "guarded by STATUS3_ON_AIR, which the slice cannot hold"),
    "damagesAirborne": (
        "src/battle_script_commands.c:1578",
        "only relaxes the hit check for a STATUS3_ON_AIR target"),
    "damagesUnderwater": (
        "src/battle_util.c:10019",
        "guarded by STATUS3_UNDERWATER, which the slice cannot hold"),
    "damagesUnderground": (
        "src/battle_util.c:10012",
        "guarded by STATUS3_UNDERGROUND, which the slice cannot hold"),
    "thawsUser": (
        "src/battle_util.c:3214",
        "guarded by STATUS1_FREEZE/FROSTBITE, and the slice requires status none"),
    "forcePressure": (
        "src/battle_script_commands.c:1881",
        "only adds PP deduction against ABILITY_PRESSURE; no PP is spent inside the window"),
    "ignoresProtect": (
        "src/battle_util.c:8530",
        "only bypasses a Protect state, which the slice has none of"),
    "ignoresSubstitute": (
        "src/battle_script_commands.c:15133",
        "only bypasses a Substitute, which the slice has none of"),
    "magicCoatAffected": (
        "src/battle_script_commands.c:1353",
        "only bounces through Magic Coat, which the slice has none of"),
    "snatchAffected": (
        "src/battle_script_commands.c:1411",
        "only redirects through Snatch, which the slice has none of"),
    # -- read only by the move-copying / format meta mechanics outside the window --
    "mirrorMoveBanned": ("src/battle_script_commands.c:6376", "Mirror Move only"),
    "metronomeBanned": ("src/battle_script_commands.c:12894", "Metronome only"),
    "assistBanned": ("src/battle_script_commands.c:14864", "Assist only"),
    "sketchBanned": ("src/battle_script_commands.c:13246", "Sketch only"),
    "sleepTalkBanned": ("src/battle_script_commands.c:13298", "Sleep Talk only"),
    "instructBanned": ("src/battle_script_commands.c:10338", "Instruct only"),
    "copycatBanned": ("src/battle_script_commands.c:17104", "Copycat only"),
    "mimicBanned": ("src/battle_script_commands.c:12853", "Mimic only"),
    "encoreBanned": ("src/battle_script_commands.c:13052", "Encore only"),
    "parentalBondBanned": ("src/battle_script_commands.c:16247", "Parental Bond only"),
    "meFirstBanned": ("src/battle_script_commands.c:10214", "Me First only"),
    "gravityBanned": ("src/battle_util.c:1106", "only gated on field gravity, which the slice has none of"),
    "skyBattleBanned": ("src/battle_script_commands.c:1160", "only gated on the Sky Battle format flag"),
}

NO_ABILITY = "ABILITY_NONE"
NO_ITEM = "ITEM_NONE"

# src/data/items.h:544-680 (heal amount = holdEffectParam / ITEM4_HEAL_HP) and
# src/battle_ai_switch_items.c:2328 ShouldUseItem.  The third field is the
# revived HP as a percentage of max HP (Revive restores half, Max Revive full).
ITEM_TABLE = {
    "ITEM_POTION": ("EFFECT_ITEM_RESTORE_HP", 20, 0),
    "ITEM_SUPER_POTION": ("EFFECT_ITEM_RESTORE_HP", 60, 0),
    "ITEM_HYPER_POTION": ("EFFECT_ITEM_RESTORE_HP", 120, 0),
    "ITEM_MAX_POTION": ("EFFECT_ITEM_RESTORE_HP", 255, 0),
    "ITEM_FULL_RESTORE": ("EFFECT_ITEM_HEAL_AND_CURE_STATUS", 255, 0),
    "ITEM_REVIVE": ("EFFECT_ITEM_REVIVE", 0, 50),
    "ITEM_MAX_REVIVE": ("EFFECT_ITEM_REVIVE", 0, 100),
}


# ---------------------------------------------------------------------------
# SFC32 RNG  (include/random.h:37-96, src/random.c:161-186)
# ---------------------------------------------------------------------------

U32 = 0xFFFFFFFF


class Rng(object):
    """The ROM's SFC32 stream with an entered 4-word state."""

    __slots__ = ("a", "b", "c", "ctr")

    def __init__(self, state):
        if not isinstance(state, (list, tuple)) or len(state) != 4:
            raise ValueError("rng.state must be a list of 4 integers")
        for word in state:
            if not isinstance(word, int) or isinstance(word, bool) or not 0 <= word <= U32:
                raise ValueError("rng.state words must be unsigned 32-bit integers")
        self.a, self.b, self.c, self.ctr = state

    def next_u32(self):
        """_SFC32_Next  include/random.h:52"""
        result = (self.a + self.b + self.ctr) & U32
        self.ctr = (self.ctr + 1) & U32
        self.a = self.b ^ (self.b >> 9)
        self.b = (self.c * 9) & U32
        self.c = (result + (((self.c << 21) | (self.c >> 11)) & U32)) & U32
        return result

    def random16(self):
        """Random()  include/random.h:75  (Random32() >> 16)"""
        return self.next_u32() >> 16

    def snapshot(self):
        return [self.a, self.b, self.c, self.ctr]


def random_uniform(rng, lo, hi):
    """RandomUniformDefault  src/random.c:161"""
    return lo + (((hi - lo + 1) * rng.random16()) >> 16)


def random_weighted_array(rng, weights):
    """RandomWeightedArrayDefault  src/random.c:172 (returns the index)"""
    total = sum(weights)
    target = (total * rng.random16()) >> 16
    for i, weight in enumerate(weights[:-1]):
        target -= weight
        if target < 0:
            return i
    return len(weights) - 1


def random_percentage(rng, t):
    """RandomPercentage  include/random.h:230"""
    if t <= 0:
        return False
    if t >= 100:
        return True
    return random_weighted_array(rng, [100 - t, t]) == 1


def random_chance(rng, successes, total):
    """RandomChance  include/random.h:226"""
    return random_weighted_array(rng, [total - successes, successes]) == 1


# ---------------------------------------------------------------------------
# snapshot normalisation
# ---------------------------------------------------------------------------

_FIELD_KEYS = ("weather", "terrain", "trick_room", "player_screens",
               "enemy_screens", "player_hazards", "enemy_hazards")


def _fail(msg):
    raise ValueError(msg)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _norm_build(build, where):
    if not isinstance(build, dict):
        _fail("%s must be an object" % where)
    out = {}
    species = build.get("species")
    if species is not None and not isinstance(species, str):
        _fail("%s.species must be a string or null" % where)
    slot_id = build.get("slot_id")
    if slot_id is not None and not _is_int(slot_id):
        _fail("%s.slot_id must be an integer or null" % where)
    out["slot_id"] = slot_id
    out["species"] = species
    for key in ("level", "max_hp", "hp"):
        value = build.get(key)
        if value is not None and not _is_int(value):
            _fail("%s.%s must be an integer or null" % (where, key))
        out[key] = value
    for key in ("ability", "item", "nature", "status"):
        value = build.get(key)
        if value is not None and not isinstance(value, str):
            _fail("%s.%s must be a string or null" % (where, key))
        out[key] = value
    for key in ("ivs", "evs", "stat_stages"):
        value = build.get(key)
        if value is None:
            value = {}
        if not isinstance(value, dict):
            _fail("%s.%s must be an object" % (where, key))
        clean = {}
        for stat, number in value.items():
            if stat not in STATS:
                _fail("%s.%s has unknown stat %r" % (where, key, stat))
            if not _is_int(number):
                _fail("%s.%s.%s must be an integer" % (where, key, stat))
            clean[stat] = number
        out[key] = clean
    moves = build.get("moves")
    if moves is None:
        moves = []
    if (not isinstance(moves, list) or len(moves) > 4
            or not all(isinstance(m, str) for m in moves)):
        _fail("%s.moves must be a list of at most 4 move id strings" % where)
    if len(set(moves)) != len(moves):
        _fail("%s.moves must not contain duplicates" % where)
    out["moves"] = list(moves)
    pp = build.get("pp")
    if pp is None:
        pp = {}
    if not isinstance(pp, dict):
        _fail("%s.pp must be an object" % where)
    clean_pp = {}
    for move_id, value in pp.items():
        if not isinstance(move_id, str):
            _fail("%s.pp keys must be move id strings" % where)
        if value is not None and not _is_int(value):
            _fail("%s.pp[%s] must be an integer or null" % (where, move_id))
        clean_pp[move_id] = value
    out["pp"] = clean_pp
    use_item = build.get("use_item")
    if use_item is None:
        use_item = False
    if not isinstance(use_item, bool):
        _fail("%s.use_item must be a boolean" % where)
    out["use_item"] = use_item
    return out


def _norm_side(side, where, with_trainer):
    if not isinstance(side, dict):
        _fail("%s must be an object" % where)
    party = side.get("party")
    if not isinstance(party, list):
        _fail("%s.party must be a list" % where)
    active = side.get("active_index")
    if not _is_int(active) or active < 0 or (party and active >= len(party)):
        _fail("%s.active_index must index into party" % where)
    out = {}
    if with_trainer:
        trainer_id = side.get("trainer_id")
        if not isinstance(trainer_id, str):
            _fail("%s.trainer_id must be a string" % where)
        items = side.get("items")
        if items is None:
            items = {}
        if not isinstance(items, dict):
            _fail("%s.items must be an object" % where)
        clean_items = {}
        for key, value in items.items():
            if not isinstance(key, str) or not _is_int(value):
                _fail("%s.items must map item id strings to integers" % where)
            clean_items[key] = value
        flags = side.get("ai_flags")
        if flags is None:
            flags = []
        if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
            _fail("%s.ai_flags must be a list of strings" % where)
        out["trainer_id"] = trainer_id
        out["items"] = clean_items
        out["ai_flags"] = list(flags)
    out["party"] = [_norm_build(b, "%s.party[%d]" % (where, i)) for i, b in enumerate(party)]
    out["active_index"] = active
    return out


def _norm_action(action, where):
    if action is None:
        return None
    if not isinstance(action, dict):
        _fail("%s must be an object or null" % where)
    kind = action.get("kind")
    if kind not in ("move", "switch"):
        _fail("%s.kind must be 'move' or 'switch'" % where)
    out = {"kind": kind}
    if kind == "move":
        move_id = action.get("move_id")
        if not isinstance(move_id, str):
            _fail("%s.move_id must be a string" % where)
        target = action.get("target_index")
        if target is not None and not _is_int(target):
            _fail("%s.target_index must be an integer or null" % where)
        out["move_id"] = move_id
        out["target_index"] = target
    else:
        slot = action.get("slot")
        if not _is_int(slot):
            _fail("%s.slot must be an integer" % where)
        out["slot"] = slot
    return out


def normalize_snapshot(snapshot):
    """Canonical copy of a BattleSnapshot; raises ValueError if malformed."""

    if not isinstance(snapshot, dict):
        _fail("snapshot must be an object")
    version = snapshot.get("schema_version")
    if not _is_int(version) or version != 1:
        _fail("schema_version must be 1")
    source_sha = snapshot.get("source_sha")
    if not isinstance(source_sha, str):
        _fail("source_sha must be a string")
    difficulty = snapshot.get("difficulty")
    if not isinstance(difficulty, str):
        _fail("difficulty must be a string")

    progress = snapshot.get("progress") or {}
    if not isinstance(progress, dict):
        _fail("progress must be an object")
    milestone = progress.get("milestone_id")
    if milestone is not None and not isinstance(milestone, str):
        _fail("progress.milestone_id must be a string or null")
    flags = progress.get("flags") or {}
    if (not isinstance(flags, dict)
            or not all(isinstance(k, str) and isinstance(v, bool) for k, v in flags.items())):
        _fail("progress.flags must map flag ids to booleans")

    player = _norm_side(snapshot.get("player"), "player", with_trainer=False)
    enemy = _norm_side(snapshot.get("enemy"), "enemy", with_trainer=True)

    field = snapshot.get("field") or {}
    if not isinstance(field, dict):
        _fail("field must be an object")
    clean_field = {key: field.get(key) for key in _FIELD_KEYS}

    rng = snapshot.get("rng") or {}
    if not isinstance(rng, dict):
        _fail("rng must be an object")
    state = rng.get("state")
    if state is not None:
        if (not isinstance(state, (list, tuple)) or len(state) != 4
                or not all(_is_int(w) and 0 <= w <= U32 for w in state)):
            _fail("rng.state must be null or a list of 4 unsigned 32-bit integers")
        state = list(state)

    history = snapshot.get("history") or {}
    if not isinstance(history, dict):
        _fail("history must be an object")
    turns = history.get("turns")
    if turns is None:
        turns = []
    if not isinstance(turns, list):
        _fail("history.turns must be a list")
    for i, turn in enumerate(turns):
        if not isinstance(turn, dict):
            _fail("history.turns[%d] must be an object" % i)
        _norm_action(turn.get("player_action"), "history.turns[%d].player_action" % i)
        _norm_action(turn.get("enemy_action"), "history.turns[%d].enemy_action" % i)
        events = turn.get("events") or []
        if not isinstance(events, list):
            _fail("history.turns[%d].events must be a list" % i)

    return {
        "schema_version": 1,
        "source_sha": source_sha,
        "difficulty": difficulty,
        "progress": {"milestone_id": milestone, "flags": dict(flags)},
        "player": player,
        "enemy": enemy,
        "field": clean_field,
        "rng": {"state": state},
        "history": {"turns": list(turns)},
        "planned_action": _norm_action(snapshot.get("planned_action"), "planned_action"),
    }


# ---------------------------------------------------------------------------
# fixed point, stats, damage
# ---------------------------------------------------------------------------

def half_down(modifier, value):
    """uq4_12_multiply_by_int_half_down  include/fpmath.h:70"""
    return (modifier * value + UQ_4_12_ONE // 2 - 1) >> 12


def mul_uq_4_12(a, b):
    """uq4_12_multiply  include/fpmath.h:50"""
    return (a * b + UQ_4_12_ONE // 2) >> 12


def to_uq_4_12(value):
    """UQ_4_12(n)  include/fpmath.h:15"""
    return int(float(value) * UQ_4_12_ONE + 0.5)


def nature_modifier(nature, stat):
    """ModifyStatByNature  src/pokemon.c:5486"""
    if nature not in NATURES:
        return 0
    up, down = NATURES[nature]
    if up == down:
        return 0
    if up == stat:
        return 1
    if down == stat:
        return -1
    return 0


def calc_mon_stats(base_stats, level, nature, ivs, evs):
    """CalculateMonStats  src/pokemon.c:2065 (FLAG_MIN_GRINDING_MODE unset)"""
    out = {}
    for stat in STATS:
        base = base_stats[stat]
        iv = ivs[stat]
        ev = evs[stat]
        if stat == "hp":
            out[stat] = ((2 * base + iv + ev // 4) * level) // 100 + level + 10
        else:
            value = ((2 * base + iv + ev // 4) * level) // 100 + 5
            mod = nature_modifier(nature, stat)
            if mod > 0:
                value = value * 110 // 100
            elif mod < 0:
                value = value * 90 // 100
            out[stat] = value
    return out


def _base_damage(power, attack, level, defense):
    """CalculateBaseDamage  src/battle_util.c:9895"""
    return (power * attack * (2 * level // 5 + 2)) // defense // 50 + 2


def _type_modifier(move_type, def_types, chart):
    """MulByTypeEffectiveness / CalcTypeEffectivenessMultiplierInternal

    src/battle_util.c:10433 / :10537 with no abilities, stages or field effects.
    """
    modifier = UQ_4_12_ONE
    previous = None
    for def_type in def_types:
        if def_type == previous:
            continue
        previous = def_type
        modifier = mul_uq_4_12(modifier, to_uq_4_12(chart[move_type][def_type]))
    return modifier


def _stab_modifier(attacker_types, move_type, move_id):
    """GetSameTypeAttackBonusModifier  src/battle_util.c:9975 (no Adaptability)"""
    if move_type == "TYPE_MYSTERY" or move_id == "MOVE_STRUGGLE":
        return UQ_4_12_ONE
    return STAB_MULTIPLIER if move_type in attacker_types else UQ_4_12_ONE


def _hit_damage(move, attacker, defender, ctx, is_crit, roll):
    """DoMoveDamageCalcVars + ApplyModifiersAfterDmgRoll for the supported slice.

    src/battle_util.c:10262 and :10294.  With no abilities/items/statuses/
    stages/weather every modifier except crit, STAB and the type chart is
    UQ_4_12(1.0) and therefore an exact identity.
    """
    type_modifier = _type_modifier(move["type"], defender["types"], ctx["chart"])
    if type_modifier == 0:                                  # DoMoveDamageCalc early return
        return 0
    physical = move["category"] == "physical"
    attack = attacker["stats"]["attack" if physical else "sp_attack"]
    defense = defender["stats"]["defense" if physical else "sp_defense"]
    damage = _base_damage(move["power"], attack, attacker["level"], defense)
    damage = half_down(CRIT_MULTIPLIER if is_crit else UQ_4_12_ONE, damage)
    damage = damage * (DMG_ROLL_HI - roll) // 100           # src/battle_util.c:10284
    damage = half_down(_stab_modifier(attacker["types"], move["type"], move["id"]), damage)
    damage = half_down(type_modifier, damage)
    return damage if damage else 1                          # src/battle_util.c:10292


def _ai_damage(attacker, defender, move, ctx, roll_type):
    """AI_GetDamage / AI_CalcDamage with randomFactor FALSE

    src/battle_ai_util.c:39, :551, :616, :721 and src/battle_util.c:10286.
    """
    type_modifier = _type_modifier(move["type"], defender["types"], ctx["chart"])
    if move["power"] == 0 or type_modifier == 0:            # IsDamageMoveUnusable
        return 0
    physical = move["category"] == "physical"
    attack = attacker["stats"]["attack" if physical else "sp_attack"]
    defense = defender["stats"]["defense" if physical else "sp_defense"]
    damage = _base_damage(move["power"], attack, attacker["level"], defense)
    if move["always_critical"] or move["crit_ratio"] >= 2:  # ShouldCalcCritDamage
        damage = half_down(CRIT_MULTIPLIER, damage)
    if damage == 0:
        damage = 1
    percent = {"min": DMG_ROLL_LO, "med": DMG_ROLL_DEFAULT_PERCENT, "max": DMG_ROLL_HI}[roll_type]
    damage = damage * percent // 100
    damage = half_down(_stab_modifier(attacker["types"], move["type"], move["id"]), damage)
    damage = half_down(type_modifier, damage)
    if move["effect"] == "EFFECT_MULTI_HIT":                # CalcDynamicMoveDamage
        damage *= 3
    if move["strike_count"] > 1 and move["effect"] != "EFFECT_TRIPLE_KICK":
        damage *= move["strike_count"]
    return damage if damage else 1


# ---------------------------------------------------------------------------
# gating: what can this engine actually answer?
# ---------------------------------------------------------------------------

class _Gate(object):
    def __init__(self):
        self.missing = []
        self.moves = []
        self.abilities = []
        self.items = []
        self.mechanics = []
        self.conditional = []
        self.difficulty = []
        self.history = []

    def result(self):
        if self.missing:
            return ("missing-input",
                    "snapshot is missing inputs required for an exact result",
                    list(self.missing), [])
        for code, ids in (("unsupported-move", self.moves),
                          ("unsupported-ability", self.abilities),
                          ("unsupported-item", self.items),
                          ("unrepresentable-history", self.history),
                          ("conditional-party", self.conditional),
                          ("unsupported-difficulty", self.difficulty),
                          ("unsupported-mechanic", self.mechanics)):
            if ids:
                return (code,
                        "state uses mechanics this engine has not ported from source",
                        [], list(ids))
        return None

    def absorb(self, other):
        """Merge another gate's diagnostics into this one."""
        for name in ("missing", "moves", "abilities", "items", "mechanics",
                     "conditional", "difficulty", "history"):
            getattr(self, name).extend(getattr(other, name))
        return self

    def copy(self):
        return _Gate().absorb(self)

    def empty(self):
        return not (self.missing or self.moves or self.abilities or self.items
                    or self.mechanics or self.conditional or self.difficulty
                    or self.history)


# _Gate.result() priority, reused when merging per-entry failures
_REASON_ORDER = ("missing-input", "unsupported-move", "unsupported-ability",
                 "unsupported-item", "unrepresentable-history", "conditional-party",
                 "unsupported-difficulty", "unsupported-mechanic")


def _worst_reason(reasons):
    for reason in _REASON_ORDER:
        if reason in reasons:
            return reason
    return reasons[0]


def _is_empty_slot(move_id):
    """``MOVE_NONE`` is the ROM's empty moveslot, not a move (include/constants/moves.h)."""
    return move_id == "MOVE_NONE"


def _check_move(move_id, gd, gate, where):
    move = (gd.get("moves") or {}).get(move_id)
    if not isinstance(move, dict):
        gate.moves.append("%s(%s)" % (where, move_id))
        return None
    for key in ("category", "effect", "power", "accuracy", "priority", "type",
                "always_critical", "crit_ratio"):
        if move.get(key) is None:
            gate.missing.append("game_data.moves[%s].%s" % (move_id, key))
            return None
    if move["category"] not in ("physical", "special"):
        gate.moves.append("category=%s" % move["category"])
        return None
    if move["effect"] not in SUPPORTED_EFFECTS:
        gate.moves.append("%s" % move["effect"])
        return None
    if not _is_int(move["power"]) or move["power"] <= 0:
        gate.moves.append("power=%r" % (move["power"],))
        return None
    if not _is_int(move["accuracy"]) or not 0 <= move["accuracy"] <= 100:
        gate.moves.append("accuracy=%r" % (move["accuracy"],))
        return None
    if not _is_int(move["priority"]):
        gate.moves.append("priority=%r" % (move["priority"],))
        return None
    if not isinstance(move["always_critical"], bool):
        gate.moves.append("always_critical=%r" % (move["always_critical"],))
        return None
    if move["always_critical"]:
        gate.moves.append("always-critical:%s" % move_id)
        return None
    if not _is_int(move["crit_ratio"]) or not 0 <= move["crit_ratio"] <= 4:
        gate.moves.append("crit_ratio=%r" % (move["crit_ratio"],))
        return None
    strike = move.get("strike_count")
    if strike is None and move["effect"] == "EFFECT_MULTI_HIT":
        # MovesInfo.strikeCount "Defaults to 1 if not set" include/pokemon.h:512
        strike = 1
    if not _is_int(strike) or strike < 1:
        gate.missing.append("game_data.moves[%s].strike_count" % move_id)
        return None
    secondary = move.get("secondary_effects")
    if secondary is None:
        gate.missing.append("game_data.moves[%s].secondary_effects" % move_id)
        return None
    if not isinstance(secondary, list):
        gate.moves.append("secondary_effects=%r" % (secondary,))
        return None
    if secondary:
        gate.moves.append("secondary-effects:%s" % move_id)
        return None
    recoil = move.get("recoil")
    if recoil is None:
        gate.missing.append("game_data.moves[%s].recoil" % move_id)
        return None
    if not _is_int(recoil):
        gate.moves.append("recoil=%r" % (recoil,))
        return None
    if recoil != 0:
        # the user takes recoil damage (src/battle_script_commands.c:6006-6012),
        # which changes the attacker's HP inside the window
        gate.moves.append("recoil:%s=%d" % (move_id, recoil))
        return None
    flags = move.get("flags")
    if flags is None:
        gate.missing.append("game_data.moves[%s].flags" % move_id)
        return None
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
        gate.moves.append("flags=%r" % (flags,))
        return None
    for flag in flags:
        if flag not in INERT_MOVE_FLAGS:
            gate.moves.append("flag:%s" % flag)
            return None
    out = dict(move)
    out["id"] = move_id
    out["strike_count"] = strike
    return out


def _check_ability(ability_id, gate, where):
    """An ability is supported only when INERT_ABILITIES proves it inert.

    Unknown (``None``) is ``missing-input``; any other id is
    ``unsupported-ability`` naming it.
    """
    if ability_id is None:
        gate.missing.append("%s.ability" % where)
        return False
    if ability_id == NO_ABILITY or ability_id in INERT_ABILITIES:
        return True
    gate.abilities.append(ability_id)
    return False


def _probe_move(move_id, gd, where):
    """(resolved move, failure tuple) for one moveset slot.

    A failure is kept per move rather than per member so the move cards can gate
    each entry on its own (contract section 3).
    """
    gate = _Gate()
    move = _check_move(move_id, gd, gate, where)
    if move is None:
        return None, gate.result()
    return move, None


def _fainted_member(species, build):
    return {"fainted": True, "revived": False, "species": species, "id": species,
            "name": species, "types": [], "stats": None, "level": None,
            "max_hp": None, "hp": 0, "moves": [], "unsupported_moves": [],
            "pp": {}, "build": build}


def _resolve_member(build, gd, gate, where, level_adjust=0):
    """Fully resolve one party slot; ``None`` when it cannot be evaluated.

    ``level_adjust`` is the Easy-mode boss-level delta
    (``src/battle_main.c:2075`` / ``:2087``) applied before stats are computed.
    Member-level problems are written to ``gate``; a *move* this engine cannot
    evaluate does not reject the member, it is listed in ``unsupported_moves``
    so each card can gate that entry on its own.
    """
    species = build["species"]
    if species is None or species == "SPECIES_NONE":
        return None                     # empty slot: not a Pokémon
    hp = build["hp"]
    if hp is None:
        gate.missing.append("%s.hp" % where)
        return None
    if hp == 0:
        # a fainted member cannot act or be switched in, so it needs nothing else
        return _fainted_member(species, build)
    local = _Gate()
    for key in ("level", "max_hp", "nature"):
        if build[key] is None:
            local.missing.append("%s.%s" % (where, key))
    _check_ability(build["ability"], local, where)
    # `item` is the held item (BUILD, contract section 2).  Held items are not
    # ported; with `use_item` false a null item means "no held item", with
    # `use_item` true it means "unknown bag item" -> missing-input.
    item = build["item"]
    if item not in (None, NO_ITEM):
        local.items.append(item)
    elif build["use_item"] and item is None:
        local.missing.append("%s.item" % where)
    status = build["status"]
    if status is None:
        local.missing.append("%s.status" % where)
    elif status != "none":
        local.mechanics.append("status:%s" % status)
    for stat, value in build["stat_stages"].items():
        if value != 0:
            local.mechanics.append("stat-stage:%s=%d" % (stat, value))
    for stat in STATS:
        if stat not in build["ivs"]:
            local.missing.append("%s.ivs.%s" % (where, stat))
        if stat not in build["evs"]:
            local.missing.append("%s.evs.%s" % (where, stat))
    if not local.empty():
        gate.absorb(local)
        return None
    species_data = (gd.get("species") or {}).get(species)
    if not isinstance(species_data, dict) or not species_data.get("types"):
        gate.mechanics.append("species:%s" % species)
        return None
    level = build["level"]
    if level_adjust:
        # ponytail: the ROM does not clamp this; clamping to 1 keeps CreateMon
        # from seeing a non-positive level.  Upgrade when a real fixture shows it.
        level = max(1, level + level_adjust)
    stats = calc_mon_stats(species_data["stats"], level, build["nature"],
                           build["ivs"], build["evs"])
    # Pinned ROM data has enough provenance to reject an impossible max HP.
    # Small synthetic tables used by isolated engine tests omit source metadata.
    if isinstance(gd.get("source"), dict) and build["max_hp"] != stats["hp"]:
        gate.mechanics.append("%s.max_hp=%s (expected %s at level %s)"
                              % (where, build["max_hp"], stats["hp"], level))
        return None
    pp = dict(build["pp"] or {})
    moves = []
    unsupported = []
    for move_id in build["moves"]:
        if _is_empty_slot(move_id):
            continue                       # gBattleMons[].moves[i] == MOVE_NONE
        move, failure = _probe_move(move_id, gd, where)
        if move is None:
            if pp.get(move_id) == 0:
                # CheckMoveLimitations zeroes a 0-PP slot (src/battle_util.c:1372),
                # so the AI never scores or picks it and it is never used: an
                # unported move in such a slot cannot reach any card.
                continue
            unsupported.append({"move_id": move_id, "failure": failure})
            continue
        moves.append(move)
    return {
        "fainted": False, "revived": False, "species": species,
        "id": species, "name": species_data.get("name", species),
        "types": list(species_data["types"]), "stats": stats,
        "level": level, "max_hp": build["max_hp"], "hp": hp,
        "moves": moves, "unsupported_moves": unsupported,
        "pp": {move_id: pp.get(move_id) for move_id in build["moves"]},
        "build": build,
    }


def _bench_score_member(build):
    """A bench member that can only contribute its constant score term.

    Used for the enemy bench once the switch decision is proven FALSE: the member
    cannot act or be reached inside the two-turn window, so the only thing it can
    change is ``100*HP/maxHP``.  When its HP was not entered that term is unknown,
    so the member is *excluded* -- never treated as zero -- and the caller notes
    the exclusion instead of inventing a value.
    """
    species = build["species"]
    if species is None or species == "SPECIES_NONE":
        return None
    hp, max_hp = build["hp"], build["max_hp"]
    if hp is None or max_hp is None:
        return None
    if hp == 0:
        return _fainted_member(species, build)
    return {"fainted": False, "revived": False, "species": species, "id": species,
            "name": species, "types": [], "stats": None, "level": None,
            "max_hp": max_hp, "hp": hp, "moves": [], "unsupported_moves": [],
            "pp": {}, "build": build}


def _party_for_card(side, gd, gate, label, active_index, level_adjust=0, bench="skip"):
    """Resolve exactly the slots one card walks.

    ``bench`` is ``"skip"`` (the card never reads a bench), ``"full"`` (every
    other slot is a candidate the card must evaluate) or ``"score"`` (only the
    constant score term is needed, so unknown members are left out).
    """
    party = [None] * len(side["party"])
    for index, build in enumerate(side["party"]):
        where = "%s.party[%d]" % (label, index)
        if index == active_index:
            party[index] = _resolve_member(build, gd, gate, where, level_adjust)
        elif bench == "full":
            party[index] = _resolve_member(build, gd, gate, where, level_adjust)
        elif bench == "score":
            party[index] = _bench_score_member(build)
    return party


def _bench_requirement(snap, gd, player_types, items=None):
    """What the enemy bench imposes on this turn, when it is reachable at all.

    Returns ``(missing, mechanics, indices)``; an empty triple means the bench is
    provably irrelevant, so no bench input is required.  Three source facts drive
    it (all from the pinned ``src/battle_ai_switch_items.c``):

    * ``GetMostSuitableMonToSwitchInto`` (:2212, called for every AI turn at
      ``src/battle_main.c:4384``, before the move-selection draws at ``:4390``)
      filters the party into ``invalidMons`` and ``aliveCount`` (:2268-:2288) and
      then calls ``GetBestMonBatonPass`` (:2291).  That helper consumes RNG only
      inside ``if ((aliveCount == 2 || (aliveCount > 2 && Random() % 3 == 0)) && bits)``
      (:1341), where ``bits`` are the valid candidates that know Baton Pass
      (:1327-:1338).  So a party that can hold 3+ valid candidates spends one
      ``Random() % 3`` even with no Baton Pass holder, and 2 candidates with a
      live Baton Pass holder spend the two do-while draws -- neither is modelled, so
      both fail closed.  ``IsAceMon`` (:111) needs an AI flag set this engine does
      not support, and ``IsTruantMonVulnerable`` (``src/battle_ai_util.c:359``) only
      reads the opponent's Protect/semi-invulnerable history, so neither removes a
      candidate here.
    * ``ShouldSwitchIfAllMovesBad`` (:388, reached from ``ShouldSwitch`` :1018) is
      the only non-``AI_FLAG_SMART_SWITCHING`` switch branch and returns FALSE as
      soon as one active move slot is a usable damaging move that affects the foe
      (:410-:417).  Otherwise a valid bench member with an effective damaging move
      is switched in (:419-:421, ``FindMonWithMoveOfEffectiveness`` :356).
    * With an inert player ability, ``CanAbilityAbsorbMove``/``CanAbilityBlockMove``
      (:414-:415) cannot block anything, so the type chart alone decides.

    ``items`` is the side's supported bag items: a Revive restores a fainted party
    member, which adds a candidate (and ``Random() % 3`` draw) on the following
    turn, so such a member counts as a possible candidate.
    """
    e_index = snap["enemy"]["active_index"]
    party = snap["enemy"]["party"]
    slots = [(index, build) for index, build in enumerate(party)
             if index != e_index and build["species"] not in (None, "SPECIES_NONE")]
    if not slots or not player_types:
        return [], [], []
    live = [index for index, build in slots
            if build["hp"] is not None and build["hp"] > 0]
    unknown = [index for index, build in slots if build["hp"] is None]
    resurrectable = []
    if items and any(ITEM_TABLE[item_id][0] == "EFFECT_ITEM_REVIVE" for item_id in items):
        resurrectable = [index for index, build in slots if build["hp"] == 0]
    if len(live) + len(resurrectable) >= 3:
        return [], ["ai-switching:switch-in-rng"], list(live + resurrectable)
    if unknown and len(live) + len(unknown) + len(resurrectable) >= 3:
        return ["enemy.party[%d].hp" % index for index in unknown], [], list(unknown)
    baton = [index for index, build in slots
             if "MOVE_BATON_PASS" in (build["moves"] or [])]
    if baton:
        unresolved = [index for index in baton if party[index]["hp"] is None]
        if unresolved:
            return ["enemy.party[%d].hp" % index for index in unresolved], [], list(unresolved)
        if [index for index in baton if party[index]["hp"] > 0]:
            return [], ["ai-switching:baton-pass-switch-in"], list(baton)
    for move_id in party[e_index]["moves"]:
        if move_id == "MOVE_NONE":
            continue
        move = (gd.get("moves") or {}).get(move_id)
        if not isinstance(move, dict) or move.get("power") is None or move.get("type") is None:
            return [], ["ai-switching:unresolved-active-move"], []
        if move["power"] == 0:
            continue                       # IS_MOVE_STATUS
        if _type_modifier(move["type"], player_types, gd["type_chart"]) > 0:
            return [], [], []              # a move that affects the foe -> no switch
    if unknown:
        return ["enemy.party[%d].hp" % index for index in unknown], [], list(unknown)
    if not live and not resurrectable:
        return [], [], []                  # every candidate invalid -> no switch
    return [], ["ai-switching:ShouldSwitchIfAllMovesBad"], list(live + resurrectable)


def _prepare(snap, gd):
    """(shared, gate) -- the inputs *every* card reads, and their failures.

    ``gate`` blocks all four cards because each entry is genuinely read by each
    card: the pinned source sha, the game_data tables, the field block (weather
    and screens change damage, trick room changes order), the difficulty level
    adjustment, the trainer record (conditional party, Easy boss level), the AI
    flag set (it fixes the enemy's action, which every card models) and the
    trainer's bag items (they extend the enemy's action space). Enemy bench
    reachability is applied per card by ``_action_gate``.
    """
    gate = _Gate()
    if snap["source_sha"] != SOURCE_SHA:
        return None, ("source-mismatch",
                      "snapshot source_sha %r does not match the pinned %s"
                      % (snap["source_sha"], SOURCE_SHA), [], [])
    if snap["difficulty"] not in ("Normal", "Easy"):
        gate.difficulty.append("difficulty=%s" % snap["difficulty"])
    if not isinstance(gd, dict):
        gate.mechanics.append("game_data")
        return None, gate.result()
    data_source = gd.get("source")
    if isinstance(data_source, dict) and data_source.get("commit") not in (None, SOURCE_SHA):
        return None, ("source-mismatch",
                      "game_data source commit %r does not match the pinned %s"
                      % (data_source["commit"], SOURCE_SHA), [], [])
    for key in ("moves", "species", "type_chart"):
        if not isinstance(gd.get(key), dict):
            gate.mechanics.append("game_data.%s" % key)
    field = snap["field"]
    for key in ("weather", "terrain", "player_screens", "enemy_screens",
                "player_hazards", "enemy_hazards"):
        if field.get(key) is not None:
            gate.mechanics.append("field.%s=%r" % (key, field[key]))
    trick_room = field.get("trick_room")
    if trick_room not in (None, False, True):
        gate.mechanics.append("field.trick_room=%r" % (trick_room,))
    unknown_flags = [f for f in snap["enemy"]["ai_flags"] if f not in SUPPORTED_AI_FLAGS]
    if unknown_flags:
        gate.mechanics.extend("ai-flag:%s" % f for f in unknown_flags)
    elif len(snap["enemy"]["ai_flags"]) > len(SUPPORTED_AI_FLAGS):
        gate.mechanics.append("ai-flag-set:%s" % ",".join(snap["enemy"]["ai_flags"]))

    trainer_id = snap["enemy"]["trainer_id"]
    trainers = gd.get("trainers")
    trainer = None
    if isinstance(trainers, list):
        for entry in trainers:
            if isinstance(entry, dict) and entry.get("id") == trainer_id:
                trainer = entry
                break
    elif isinstance(trainers, dict):
        trainer = trainers.get(trainer_id)
    if trainer is None:
        gate.missing.append("enemy.trainer_id")
    elif trainer.get("conditional_party"):
        gate.conditional.append("conditional-party:%s" % trainer_id)

    level_adjust = 0
    if snap["difficulty"] == "Easy":
        is_boss = trainer.get("is_boss") if isinstance(trainer, dict) else None
        if not isinstance(is_boss, bool):
            gate.difficulty.append("Easy:unresolved-boss")
        elif is_boss:
            level_adjust = -2

    items = {}
    for item_id, count in snap["enemy"]["items"].items():
        if count <= 0:
            continue
        if item_id not in ITEM_TABLE:
            gate.items.append(item_id)
        else:
            items[item_id] = count
    if snap["history"]["turns"]:
        gate.history.append("history.turns:seed-posterior-not-implemented")
    if items and snap["history"]["turns"]:
        # ShouldUseItem reads AI_DATA->lastUsedMove (battle history); an entered
        # log we do not model makes the item decision unrepresentable.
        gate.history.append("trainer-item-ai-history")

    player_types = None
    p_index = snap["player"]["active_index"]
    if p_index < len(snap["player"]["party"]):
        species = (gd.get("species") or {}).get(snap["player"]["party"][p_index]["species"])
        if isinstance(species, dict):
            player_types = species.get("types")
    bench_missing, bench_mechanics, _ = _bench_requirement(snap, gd, player_types, items)

    shared = {
        "gd": gd, "chart": gd["type_chart"], "moves": gd["moves"],
        "items": items, "level_adjust": level_adjust,
        "difficulty": snap["difficulty"],
        "trick_room": bool(trick_room),
        "p_index": p_index, "e_index": snap["enemy"]["active_index"],
        # AI_CheckBadMove runs only when its bit is set in aiFlags;
        # sBattleAiFuncTable / ChooseMoveOrAction_Singles src/battle_ai_main.c:138, :492
        "ai_check_bad_move": "AI_FLAG_CHECK_BAD_MOVE" in snap["enemy"]["ai_flags"],
        "bench_missing": bench_missing,
        "bench_mechanics": bench_mechanics,
    }
    return shared, gate


def _action_gate(base, shared):
    """Add enemy switch uncertainty only to cards that select an AI action."""
    gate = base.copy()
    gate.missing.extend(shared["bench_missing"])
    gate.mechanics.extend(shared["bench_mechanics"])
    return gate


# ---------------------------------------------------------------------------
# legal actions
# ---------------------------------------------------------------------------

def legal_player_actions(snapshot, game_data):
    """{"moves": [...], "switches": [...]} for the player's active Pokémon."""
    snap = normalize_snapshot(snapshot)
    gd = game_data or {}
    moves = gd.get("moves") or {}
    species = gd.get("species") or {}
    party = snap["player"]["party"]
    active_index = snap["player"]["active_index"]
    if not party:
        return {"moves": [], "switches": []}
    active = party[active_index]
    out_moves = []
    for move_id in active["moves"]:
        if _is_empty_slot(move_id):
            continue
        pp = (active["pp"] or {}).get(move_id)
        if pp == 0:
            continue
        info = moves.get(move_id) or {}
        out_moves.append({
            "move_id": move_id,
            "move_name": info.get("name", move_id),
            "pp": pp,
            "usable": pp is None or pp > 0,
        })
    switches = []
    for index, member in enumerate(party):
        if index == active_index:
            continue
        species_id = member["species"]
        if species_id is None or species_id == "SPECIES_NONE" or member["hp"] == 0:
            continue
        switches.append({
            "slot": index,
            "species": species_id,
            "species_name": (species.get(species_id) or {}).get("name", species_id),
        })
    return {"moves": out_moves, "switches": switches}


# ---------------------------------------------------------------------------
# level cap simulation
# ---------------------------------------------------------------------------

def simulate_party_level_cap(party, level_cap, game_data=None):
    """ROM raise-to-cap on a copy (LevelBoxesToCap  src/caps.c:128).

    Unmarked, non-empty party members are set to the cap; the input list is
    never mutated and Box levels are never touched.
    """
    if not isinstance(party, list):
        raise ValueError("party must be a list")
    if not _is_int(level_cap):
        raise ValueError("level_cap must be an integer")
    cap = min(max(level_cap, 1), MAX_LEVEL)
    out = []
    for build in party:
        if not isinstance(build, dict):
            raise ValueError("party members must be objects")
        copy = dict(build)
        species = copy.get("species")
        if species is not None and species not in ("SPECIES_NONE", "SPECIES_EGG"):
            copy["level"] = cap
        out.append(copy)
    return out


# ---------------------------------------------------------------------------
# turn simulation
# ---------------------------------------------------------------------------

_SPEED_TIE_ORDERS = (
    (0, 1, 2, 3), (0, 1, 3, 2), (0, 2, 1, 3), (0, 2, 3, 1),
    (0, 3, 1, 2), (0, 3, 2, 1), (1, 0, 2, 3), (1, 0, 3, 2),
    (1, 2, 0, 3), (1, 2, 3, 0), (1, 3, 0, 2), (1, 3, 2, 0),
    (2, 0, 1, 3), (2, 0, 3, 1), (2, 1, 0, 3), (2, 1, 3, 0),
    (2, 3, 0, 1), (2, 3, 1, 0), (3, 0, 1, 2), (3, 0, 2, 1),
    (3, 1, 0, 2), (3, 1, 2, 0), (3, 2, 0, 1), (3, 2, 1, 0),
)


def _clone(state):
    out = {}
    for side in ("player", "enemy"):
        out[side] = {
            "active": state[side]["active"],
            "party": [None if m is None else dict(m) for m in state[side]["party"]],
        }
    return out


def _usable_moves(mon, ctx):
    if mon is None or mon.get("fainted") or mon.get("stats") is None:
        return []
    out = []
    for move in mon["moves"]:
        if mon["pp"].get(move["id"]) == 0:
            continue
        out.append(move)
    return out


def _resolve_move(attacker, defender, move, rng, ctx):
    """Accuracy check, crit calc, damage calc and the multi-hit loop.

    BattleScript_EffectHit  data/battle_scripts_1.s:2809, the multi-hit counter
    src/battle_util.c:11676 and MOVEEND_MULTIHIT_MOVE
    src/battle_script_commands.c:6451.
    """
    events = []
    if _type_modifier(move["type"], defender["types"], ctx["chart"]) == 0:
        events.append({"kind": "immune", "move_id": move["id"]})
        return events
    hits = 1
    if move["effect"] == "EFFECT_MULTI_HIT":
        hits = random_weighted_array(rng, [0, 0, 7, 7, 3, 3])   # src/battle_util.c:11681
    elif move["strike_count"] > 1:
        hits = move["strike_count"]
    if move["accuracy"] != 0 and move["accuracy"] < 100:
        if not random_percentage(rng, move["accuracy"]):        # src/battle_script_commands.c:1806
            events.append({"kind": "miss", "move_id": move["id"]})
            return events
    for hit in range(hits):
        if defender["hp"] <= 0:
            break
        if move["always_critical"]:
            is_crit = True
        else:
            odds = CRIT_ODDS[min(move["crit_ratio"], len(CRIT_ODDS) - 1)]
            is_crit = random_chance(rng, 1, odds)               # src/battle_script_commands.c:2148
        roll = random_uniform(rng, 0, DMG_ROLL_HI - DMG_ROLL_LO)
        damage = _hit_damage(move, attacker, defender, ctx, is_crit, roll)
        defender["hp"] = max(0, defender["hp"] - damage)
        events.append({"kind": "damage", "move_id": move["id"], "hit": hit + 1,
                       "damage": damage, "critical": is_crit, "roll": roll})
    return events


def _hp_percent(hp, max_hp):
    return hp * 100 // max_hp if max_hp else 0


def _ai_explosion_draw(enemy_mon, rng):
    """ChooseMoveOrAction_Singles: AI_DATA->shouldConsiderExpolsion, one draw

    src/battle_ai_main.c:500 with AI_RandLessThan/GetAIExplosionChanceFromHP
    src/battle_ai_util.c:96 / :106.  No scoring function in the supported flag
    set reads the result, but the draw is always consumed.
    """
    chance = _hp_percent(enemy_mon["hp"], enemy_mon["max_hp"])
    if chance >= 90:
        val = 0
    elif chance <= 10:
        val = 229
    else:
        val = (90 - chance) * 229 // 80
    rng.random16() % 0xFF < val


def _ai_check_bad_move(enemy_mon, player_mon, move, ctx, score):
    """AI_CheckBadMove  src/battle_ai_main.c:724, supported slice only.

    Exactly one scoring rule is reachable for single-target physical/special
    ``EFFECT_HIT`` / ``EFFECT_MULTI_HIT`` moves with inert abilities, no items,
    no status, no stat stages and no field effects: the type-immunity penalty at
    ``:778``.  The remaining branches and the guard that makes each unreachable:

    * ``:738`` targeting a partner -- singles, ``IS_TARGETING_PARTNER`` is
      ``battlerAtk == battlerDef ^ BIT_FLANK`` (include/battle_ai_util.h:234).
    * ``:744`` powder moves -- needs ``gMovesInfo[move].powderMove``, and no
      damaging move in the pinned data sets it.
    * ``:747``/``:750`` semi-invulnerable or two-turn states -- neither exists in
      this engine's state (``IsTwoTurnNotSemiInvulnerableMove`` only matches
      EFFECT_SOLAR_BEAM / EFFECT_TWO_TURNS_ATTACK / EFFECT_GEOMANCY,
      src/battle_ai_util.c:2765).
    * ``:753`` Dondozo commanding -- no such state.
    * ``:758`` Focus Punch setup -- requires ``DAMAGE_CATEGORY_STATUS``.
    * ``:764`` freeze thaw -- requires ``STATUS1_FROZEN | STATUS1_FROSTBITE``.
    * ``:783``-``:910`` target ability blocks, the doubles-partner block and the
      terrain checks -- the defender's ability is inert by construction, the
      battle is singles, and terrain is None.
    * ``:986`` ``case EFFECT_HIT`` -- dead: it needs
      ``priority < 0 && AI_IsFaster(..., MOVE_NONE, ...)``, and a
      negative-priority move is always *slower* than the priority-0 MOVE_NONE
      (src/battle_ai_util.c:1158), so the branch can never score.
    * ``:1002`` EFFECT_EXPLOSION and every other effect case -- not in the
      supported effect set.
    * ``:2602`` Choice-item block -- requires a held item.
    """
    if _type_modifier(move["type"], player_mon["types"], ctx["chart"]) == 0:
        return score - 20                              # RETURN_SCORE_MINUS(20)
    return score


def _ai_max_score_moves(enemy_mon, player_mon, ctx):
    """BattleAI_SetupAIData + AI_CheckBadMove + ChooseMoveOrAction_Singles.

    Returns the tied top-scoring moves, in moveset order.  Slots that are empty
    or have 0 PP score 0 (``src/battle_ai_main.c:259``,
    ``CheckMoveLimitations`` src/battle_util.c:1372); the survivors start at
    ``AI_SCORE_DEFAULT`` and ``AI_CheckBadMove`` may lower them -- but never below 0,
    so a legal move stays selectable even when it is type-immune
    (``src/battle_ai_main.c:779``) and only an empty or 0-PP slot is dropped
    (``CheckMoveLimitations`` src/battle_util.c:1372, ``IsMoveUnusable``
    include/battle_ai_util.h:60).  The tie set is the ROM's ``consideredMoveArray``
    walk at ``src/battle_ai_main.c:534``.
    """
    scores = []
    for move in enemy_mon["moves"]:
        if enemy_mon["pp"].get(move["id"]) == 0:
            scores.append(0)
        elif ctx["ai_check_bad_move"]:
            scores.append(_ai_check_bad_move(enemy_mon, player_mon, move, ctx,
                                             AI_SCORE_DEFAULT))
        else:
            # no scoring function is called for the empty flag set, so every
            # usable move keeps AI_SCORE_DEFAULT (ChooseMoveOrAction_Singles
            # src/battle_ai_main.c:504-519)
            scores.append(AI_SCORE_DEFAULT)
    if not scores:
        return []
    best = scores[0]
    considered = [0]
    for index in range(1, len(scores)):
        if scores[index] == best:
            considered.append(index)
        elif scores[index] > best:
            best = scores[index]
            considered = [index]
    return [(enemy_mon["moves"][index], scores[index]) for index in considered]


def _ai_choose_move(enemy_mon, player_mon, rng, ctx):
    """ChooseMoveOrAction_Singles  src/battle_ai_main.c:492.

    Consumes the explosion-check draw and the ``Random() % numOfBestMoves``
    tie-break draw (``:545``).
    """
    _ai_explosion_draw(enemy_mon, rng)
    tied = _ai_max_score_moves(enemy_mon, player_mon, ctx)
    if not tied or tied[0][1] <= 0:            # nothing usable
        return None
    return tied[rng.random16() % len(tied)][0]


def _ai_should_heal(enemy_mon, heal_amount, player_mon, ctx):
    """AI_ShouldHeal  src/battle_ai_switch_items.c:2427"""
    max_hp = enemy_mon["max_hp"]
    hp = enemy_mon["hp"]
    if hp < max_hp // 4 or hp == 0 or (heal_amount != 0 and max_hp - hp > heal_amount):
        return not _ai_opponent_can_faint(enemy_mon, heal_amount, player_mon, ctx)
    return False


def _ai_opponent_can_faint(enemy_mon, heal_amount, player_mon, ctx):
    """AI_OpponentCanFaintAiWithMod + CanTargetFaintAiWithMod

    src/battle_ai_switch_items.c:2443, src/battle_ai_util.c:1427.
    """
    hp_check = min(enemy_mon["hp"] + heal_amount, enemy_mon["max_hp"])
    for move in _usable_moves(player_mon, ctx):
        if _ai_damage(player_mon, enemy_mon, move, ctx, "med") >= hp_check:
            return True
    return False


# AI_IsFaster(enemy, player, enemy_move, MOVE_NONE, CONSIDER_PRIORITY): the
# player's last-used move is MOVE_NONE (src/battle_ai_util.c:1158, :2276) so its
# priority is 0 and only the enemy move's priority sign matters.
def _ai_expects_to_faint(enemy_move, enemy_mon, player_mon, ctx):
    """AiExpectsToFaintPlayer  src/battle_ai_switch_items.c:2276"""
    if enemy_move is None:
        return False
    if _ai_damage(enemy_mon, player_mon, enemy_move, ctx, "max") < player_mon["hp"]:
        return False
    # AI_IsFaster(..., CONSIDER_PRIORITY)  src/battle_ai_util.c:1158
    if enemy_move["priority"] != 0:
        return enemy_move["priority"] > 0
    return enemy_mon["stats"]["speed"] >= player_mon["stats"]["speed"]


def _ai_should_use_item(enemy_mon, player_mon, ctx, enemy_move, enemy_party):
    """ShouldUseItem  src/battle_ai_switch_items.c:2328 (supported classes)."""
    if not ctx["items"]:
        return None
    if _ai_expects_to_faint(enemy_move, enemy_mon, player_mon, ctx):
        return None
    for item_id, count in ctx["items"].items():
        if count <= 0:
            continue
        usage, heal, revive_percent = ITEM_TABLE[item_id]
        if usage == "EFFECT_ITEM_RESTORE_HP":
            use = _ai_should_heal(enemy_mon, heal, player_mon, ctx)
        elif usage == "EFFECT_ITEM_HEAL_AND_CURE_STATUS":
            use = _ai_should_heal(enemy_mon, 0, player_mon, ctx)
        elif usage == "EFFECT_ITEM_REVIVE":
            use = any(m is not None and m.get("fainted") and not m.get("used")
                      for m in enemy_party)
        else:
            use = False
        if use:
            return (item_id, usage, heal, revive_percent)
    return None


def _speed_tie_first(speed_tie, battler_a, battler_b):
    """sBattlerOrders  src/battle_main.c:5159 (MAX_BATTLERS_COUNT == 4)."""
    order = _SPEED_TIE_ORDERS[speed_tie % 24]
    return order.index(battler_a) < order.index(battler_b)


def _acts_first_certain(move_a, mon_a, move_b, mon_b, trick_room):
    """GetWhichBattlerFasterArgs  src/battle_main.c:5053 (no items/abilities)."""
    if move_a["priority"] != move_b["priority"]:
        return move_a["priority"] > move_b["priority"]
    speed_a = mon_a["stats"]["speed"]
    speed_b = mon_b["stats"]["speed"]
    if speed_a == speed_b:
        return False
    faster = speed_a > speed_b
    if trick_room:
        faster = not faster
    return faster


def _choose_enemy_action(state, rng, ctx):
    """AI_TrySwitchOrUseItem  src/battle_ai_switch_items.c:1254.

    ShouldSwitch is FALSE for every supported state (the gate removed all living
    switch candidates), so only move / item remain.
    """
    enemy_mon = state["enemy"]["party"][state["enemy"]["active"]]
    player_mon = state["player"]["party"][state["player"]["active"]]
    move = _ai_choose_move(enemy_mon, player_mon, rng, ctx)
    item = _ai_should_use_item(enemy_mon, player_mon, ctx, move, state["enemy"]["party"])
    if item is not None:
        return ("item", item)
    if move is None:
        return None
    return ("move", move)


def _play_turn(state, player_action, rng, ctx, forced_enemy_move=None):
    """One ROM-shaped turn.  Returns (events, post_state, enemy_action)."""
    state = _clone(state)
    events = []
    speed_tie = random_uniform(rng, 0, 23)                   # src/battle_main.c:4161
    enemy_choice = _choose_enemy_action(state, rng, ctx)
    if forced_enemy_move is not None:
        enemy_choice = ("move", forced_enemy_move)
    enemy_move_data = enemy_choice[1] if enemy_choice and enemy_choice[0] == "move" else None

    # Quick Claw / Quick Draw rolls per battler with a move action, skipping
    # item/switch actions (src/battle_main.c:5267).  The Claw param is 0 with no
    # held item, so only the Quick Draw roll consumes RNG.
    for action in (player_action, enemy_choice):
        if action is None or action[0] in ("switch", "item"):
            continue
        random_percentage(rng, 0)
        random_percentage(rng, 30)                           # RNG_QUICK_DRAW

    first, second = [], []
    (first if player_action[0] == "switch" else second).append(("player", player_action))
    if enemy_choice is not None:
        (first if enemy_choice[0] == "item" else second).append(("enemy", enemy_choice))
    if len(second) == 2:
        player_mon = state["player"]["party"][state["player"]["active"]]
        enemy_mon = state["enemy"]["party"][state["enemy"]["active"]]
        p_priority = player_action[1]["priority"]
        e_priority = enemy_move_data["priority"] if enemy_move_data else 0
        if p_priority != e_priority:
            player_first = p_priority > e_priority
        elif player_mon["stats"]["speed"] != enemy_mon["stats"]["speed"]:
            faster = player_mon["stats"]["speed"] > enemy_mon["stats"]["speed"]
            player_first = (not faster) if ctx["trick_room"] else faster
        else:
            player_first = _speed_tie_first(speed_tie, 0, 1)
        if not player_first:
            second.reverse()
    order = first + second

    for side, action in order:
        if action is None:
            continue
        if side == "player":
            if action[0] == "switch":
                state["player"]["active"] = action[1]
                events.append({"kind": "switch", "side": "player", "slot": action[1]})
                continue
            attacker = state["player"]["party"][state["player"]["active"]]
            defender = state["enemy"]["party"][state["enemy"]["active"]]
        else:
            if action[0] == "item":
                item_id, usage, heal, revive_percent = action[1]
                events.append({"kind": "item", "side": "enemy", "item_id": item_id})
                if usage == "EFFECT_ITEM_REVIVE":
                    for index, member in enumerate(state["enemy"]["party"]):
                        if member is not None and member.get("fainted"):
                            member["fainted"] = False
                            member["revived"] = True
                            member["revived_percent"] = revive_percent
                            member["hp"] = 1
                            member["used"] = True
                            events.append({"kind": "revive", "slot": index})
                            break
                else:
                    enemy_mon = state["enemy"]["party"][state["enemy"]["active"]]
                    enemy_mon["hp"] = min(enemy_mon["max_hp"], enemy_mon["hp"] + heal)
                continue
            attacker = state["enemy"]["party"][state["enemy"]["active"]]
            defender = state["player"]["party"][state["player"]["active"]]
        if attacker is None or attacker.get("fainted") or attacker["hp"] <= 0:
            continue
        if defender is None or defender.get("fainted") or defender["hp"] <= 0:
            continue
        events.extend(_resolve_move(attacker, defender, action[1], rng, ctx))
    return events, state, enemy_choice


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def _mon_percent(member):
    if member is None or member.get("fainted"):
        return Fraction(0)
    if member.get("revived"):
        return Fraction(member.get("revived_percent", 50))
    max_hp = member.get("max_hp")
    if not max_hp:
        return Fraction(0)
    return Fraction(100 * member["hp"], max_hp)


def _living_count(party):
    return sum(1 for m in party if m is not None and not m.get("fainted")
               and (m.get("revived") or m.get("hp", 0) > 0))


def _score(state):
    """contract section 3:
    sum(100*HP/maxHP) player - same enemy + 100*(living player - living enemy)"""
    total = Fraction(0)
    for key, sign in (("player", 1), ("enemy", -1)):
        for member in state[key]["party"]:
            total += sign * _mon_percent(member)
    total += 100 * (_living_count(state["player"]["party"])
                    - _living_count(state["enemy"]["party"]))
    return total


# ---------------------------------------------------------------------------
# seed-independent knockout bounds
# ---------------------------------------------------------------------------

def _min_max_hits(move):
    if move["effect"] == "EFFECT_MULTI_HIT":
        return 2, 5
    return move["strike_count"], move["strike_count"]


def _min_max_damage(attacker, defender, move, ctx):
    """Worst/best case total damage for one use (crit, roll, hit-count bounds)."""
    lo_hits, hi_hits = _min_max_hits(move)
    min_hit = _hit_damage(move, attacker, defender, ctx, False, DMG_ROLL_HI - DMG_ROLL_LO)
    max_hit = _hit_damage(move, attacker, defender, ctx, True, 0)
    return lo_hits * min_hit, hi_hits * max_hit


def _solo_bound(move, attacker, defender, ctx):
    """P(KO) when nothing can stop the attacker from acting: 0, 1 or None."""
    lo, hi = _min_max_damage(attacker, defender, move, ctx)
    if hi < defender["hp"]:
        return 0
    never_miss = move["accuracy"] == 0 or move["accuracy"] >= 100
    if never_miss and lo >= defender["hp"]:
        return 1
    return None


def _immune_note(move, defender, chart):
    """Note text when the type chart alone decides this move does nothing.

    ``DoMoveDamageCalc`` (src/battle_util.c:10411) returns 0 for a
    ``UQ_4_12(0.0)`` type multiplier before any RNG is drawn, and
    ``Cmd_accuracycheck``/``JumpIfMoveFailed`` never consume a draw for it, so the
    move is *legal and selectable* with an exactly 0 chance of a knockout -- it is
    not a missing action.  ``IsMoveUnusable`` (include/battle_ai_util.h:60) is what
    marks a slot unselectable, and it only tests MOVE_NONE/MOVE_UNAVAILABLE and the
    PP/state limitation bits (``CheckMoveLimitations`` src/battle_util.c:1372) --
    type effectiveness is not part of it.
    """
    if _type_modifier(move["type"], defender["types"], chart) == 0:
        return ("the target is immune to %s (%s does 0x damage), so no draw can knock "
                "it out" % (move["id"], move["type"]))
    return None


def _ko_bound(move, attacker, defender, blockers, ctx):
    """Provable P(KO of ``defender`` by ``move``) over every RNG state.

    ``blockers`` is the list of actions the other side may take:
    ``("move", move_data, mon)``, ``("switch", slot, None)`` or ``None``
    (an action that cannot damage the attacker, e.g. a trainer item).
    Returns 0, 1 or None when the probability cannot be proven.
    """
    lo, hi = _min_max_damage(attacker, defender, move, ctx)
    if hi < defender["hp"]:
        return 0
    solo = _solo_bound(move, attacker, defender, ctx)
    bounds = []
    for blocker in blockers:
        if blocker is None:
            bounds.append(solo)
            continue
        if blocker[0] == "switch":
            bounds.append(0)        # the turn-start target left the field
            continue
        blocker_move, blocker_mon = blocker[1], blocker[2]
        if solo != 1:
            bounds.append(None)
            continue
        if _acts_first_certain(move, attacker, blocker_move, blocker_mon, ctx["trick_room"]):
            bounds.append(1)
            continue
        blocker_max = _min_max_damage(blocker_mon, attacker, blocker_move, ctx)[1]
        bounds.append(1 if blocker_max < attacker["hp"] else None)
    if all(bound == 1 for bound in bounds):
        return 1
    if all(bound == 0 for bound in bounds):
        return 0
    return None


# ---------------------------------------------------------------------------
# card helpers
# ---------------------------------------------------------------------------

def _unavailable(reason, detail, missing=None, mechanics=None):
    return {
        "status": "unavailable",
        "reason": reason,
        "detail": detail,
        "missing": list(missing or []),
        "mechanics": list(mechanics or []),
    }


def _exact(method, **fields):
    card = {
        "status": "exact",
        "source_sha": SOURCE_SHA,
        "evidence": dict(EVIDENCE_BASE),
        "method": method,
    }
    card.update(fields)
    return card


def _fraction(value):
    return {"num": value.numerator, "den": value.denominator}


def _cards_unavailable(reason, detail, missing=None, mechanics=None):
    card = _unavailable(reason, detail, missing, mechanics)
    return {
        "status": "unavailable",
        "source_sha": SOURCE_SHA,
        "engine_version": ENGINE_VERSION,
        "evidence": dict(EVIDENCE_BASE),
        "player_moves": dict(card),
        "enemy_moves": dict(card),
        "ai_actions": dict(card),
        "recommendation": dict(card),
    }


def _ctx(shared, player_party, enemy_party):
    ctx = dict(shared)
    ctx["player_party"] = player_party
    ctx["enemy_party"] = enemy_party
    return ctx


def _state(ctx):
    return {
        "player": {
            "active": ctx["p_index"],
            "party": [None if m is None else dict(m) for m in ctx["player_party"]],
        },
        "enemy": {
            "active": ctx["e_index"],
            "party": [None if m is None else dict(m) for m in ctx["enemy_party"]],
        },
    }


def _active_pair(snap, shared, gate, bench=("skip", "skip")):
    gd = shared["gd"]
    player_party = _party_for_card(snap["player"], gd, gate, "player",
                                   shared["p_index"], 0, bench[0])
    enemy_party = _party_for_card(snap["enemy"], gd, gate, "enemy",
                                  shared["e_index"], shared["level_adjust"], bench[1])
    return player_party, enemy_party


def _require_actives(gate, ctx):
    """(player_mon, enemy_mon, failure card or None) for the two actives."""
    failure = gate.result()
    if failure is not None:
        return None, None, _unavailable(*failure)
    player_mon = ctx["player_party"][ctx["p_index"]]
    enemy_mon = ctx["enemy_party"][ctx["e_index"]]
    for side, index, mon in (("player", ctx["p_index"], player_mon),
                             ("enemy", ctx["e_index"], enemy_mon)):
        if mon is None:
            return None, None, _unavailable(
                "missing-input", "the %s active slot holds no Pokémon" % side,
                missing=["%s.party[%d].species" % (side, index)])
        if mon["fainted"]:
            return None, None, _unavailable(
                "no-legal-actions", "the %s active Pokémon has fainted" % side)
    return player_mon, enemy_mon, None


def _move_name(ctx, move_id):
    return (ctx["moves"].get(move_id) or {}).get("name", move_id)


def _move_entries(mon):
    """The legal move slots in moveset order: (move_id, move, failure).

    A supported move with 0 PP is not selectable and gets no entry; an unported
    move with 0 PP was dropped in ``_resolve_member`` for the same reason
    (``CheckMoveLimitations`` src/battle_util.c:1372).
    """
    supported = {m["id"]: m for m in mon["moves"]}
    broken = {u["move_id"]: u["failure"] for u in mon["unsupported_moves"]}
    out = []
    for move_id in mon["build"]["moves"]:
        if _is_empty_slot(move_id):
            continue
        if move_id in supported:
            if mon["pp"].get(move_id) == 0:
                continue
            out.append((move_id, supported[move_id], None))
        elif move_id in broken:
            out.append((move_id, None, broken[move_id]))
    return out


def _entry(ctx, move_id, **fields):
    entry = {"move_id": move_id, "move_name": _move_name(ctx, move_id)}
    entry.update(fields)
    return entry


def _entry_exact(ctx, move_id, card):
    return _entry(ctx, move_id, status="exact", probability=card["probability"],
                  method=card["method"], ko=card.get("ko", ""),
                  notes=card.get("notes", []))


def _entry_failure(ctx, move_id, failure, notes=None):
    reason, detail, missing, mechanics = failure
    return _entry(ctx, move_id, status="unavailable", reason=reason, detail=detail,
                  missing=list(missing), mechanics=list(mechanics),
                  notes=list(notes or []))


def _entry_card(entries, key):
    """Contract section 3: exact when at least one entry is exact, else the card
    carries the merged failure of its entries (and still lists them)."""
    exact = [e for e in entries if e["status"] == "exact"]
    if exact:
        methods = {e["method"] for e in exact}
        card = _exact(methods.pop() if len(methods) == 1 else "known-seed")
        card[key] = entries
        return card
    missing, mechanics, reasons, detail = [], [], [], []
    for entry in entries:
        if entry["status"] == "exact":
            continue
        reasons.append(entry["reason"])
        missing.extend(entry.get("missing") or [])
        mechanics.extend(entry.get("mechanics") or [])
        detail.append("%s: %s" % (entry["move_id"], entry.get("detail", "")))
    if not reasons:
        return _unavailable("no-legal-actions", "no move could be evaluated")
    reason = _worst_reason(reasons)
    card = _unavailable(reason, "; ".join(detail),
                        missing if reason == "missing-input" else None, mechanics)
    card[key] = entries
    return card


def _unsupported_note(where, mon):
    """``['<where>(MOVE_X)', '<why>', ...]`` for a member's unported moves."""
    out = []
    for entry in mon["unsupported_moves"]:
        out.append("%s(%s)" % (where, entry["move_id"]))
        out.extend(entry["failure"][3] or [])
    return out


def _unsupported_failure(where, mon, detail):
    """(reason, detail, missing, mechanics) for a member's unported moves.

    The reason is the worst of the moves' own failures, so a data-provenance gap
    stays ``missing-input`` (with the dotted ``game_data`` path) instead of being
    flattened into ``unsupported-move``.
    """
    reason = _worst_reason([entry["failure"][0] for entry in mon["unsupported_moves"]])
    missing = []
    if reason == "missing-input":
        for entry in mon["unsupported_moves"]:
            if entry["failure"][0] == "missing-input":
                missing.extend(entry["failure"][2] or [])
    return (reason, detail, missing, _unsupported_note(where, mon))


def _enemy_action_set(ctx):
    """(blockers, failure) for every action the enemy AI can take this turn.

    ``AI_TrySwitchOrUseItem`` src/battle_ai_switch_items.c:1254: ``shouldSwitch`` is
    proven FALSE for supported states (``_bench_requirement``), so the enemy either
    uses a trainer item (no damage) or a move at ``BATTLE_OPPOSITE(battler)``.  An
    unported move makes that action space unknowable, which every card depends on.
    """
    enemy_mon = ctx["enemy_party"][ctx["e_index"]]
    if enemy_mon["unsupported_moves"]:
        return None, _unsupported_failure(
            "enemy.party[%d]" % ctx["e_index"], enemy_mon,
            "the enemy's active Pokémon has moves this engine has not ported")
    moves = _usable_moves(enemy_mon, ctx)
    if not moves:
        return None, ("no-legal-actions",
                      "the enemy's active Pokémon has no usable move", [], [])
    blockers = [("move", move, enemy_mon) for move in moves]
    if ctx["items"]:
        blockers.append(None)          # an item action cannot damage the player
    return blockers, None


def _planned_action(snap, ctx):
    """(player action tuple, failure) for ``snapshot['planned_action']``.

    contract section 3: ``enemy_moves`` conditions on the entered planned action,
    so the action has to be a legal one; a switch candidate needs only its own
    legality inputs (species and HP).
    """
    planned = snap["planned_action"]
    if planned is None:
        return None, ("missing-input",
                      "enemy move odds need an explicit planned player action",
                      ["planned_action"], [])
    if planned["kind"] == "switch":
        slot = planned["slot"]
        if not 0 <= slot < len(snap["player"]["party"]) or slot == ctx["p_index"]:
            return None, ("missing-input", "the planned switch target is not a valid slot",
                          ["planned_action.slot"], [])
        build = snap["player"]["party"][slot]
        if build["species"] in (None, "SPECIES_NONE"):
            return None, ("missing-input", "the planned switch target holds no Pokémon",
                          ["player.party[%d].species" % slot], [])
        if build["hp"] is None:
            return None, ("missing-input", "the planned switch target's HP is unknown",
                          ["player.party[%d].hp" % slot], [])
        if build["hp"] == 0:
            return None, ("no-legal-actions", "the planned switch target has fainted", [], [])
        return ("switch", slot), None
    for move_id, move, move_failure in _move_entries(ctx["player_party"][ctx["p_index"]]):
        if move_id == planned["move_id"]:
            if move is None:
                return None, move_failure
            return ("move", move), None
    return None, ("missing-input",
                  "the planned move is not in the player's active moveset",
                  ["planned_action.move_id"], [])


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------

def _player_moves_card(snap, shared, base, clock):
    """P(KO of the enemy active | each of the player's moves).

    Reads the two actives only (contract section 3): its blockers are the enemy's
    possible actions, never the enemy's bench or the player's bench.
    """
    gate = _action_gate(base, shared)
    player_party, enemy_party = _active_pair(snap, shared, gate)
    ctx = _ctx(shared, player_party, enemy_party)
    player_mon, enemy_mon, failure = _require_actives(gate, ctx)
    if failure is not None:
        return failure
    entries = _move_entries(player_mon)
    if not entries:
        return _unavailable("no-legal-actions",
                            "the player's active Pokémon has no selectable move: every "
                            "slot is empty or at 0 PP, so nothing can be selected")
    blockers, blocker_failure = _enemy_action_set(ctx)
    out = []
    for move_id, move, move_failure in entries:
        if move is None:
            out.append(_entry_failure(ctx, move_id, move_failure))
            continue
        if blocker_failure is not None:
            out.append(_entry_failure(ctx, move_id, blocker_failure))
            continue
        if clock.expired():
            return _unavailable("timeout", "deadline passed while evaluating player moves")
        if snap["rng"]["state"] is None:
            bound = _ko_bound(move, player_mon, enemy_mon, blockers, ctx)
            if bound is None:
                out.append(_entry_failure(ctx, move_id, (
                    "unknown-rng-seed",
                    "the knockout depends on this turn's RNG draws and no seed was entered",
                    [], [])))
                continue
            notes = ["the outcome is identical for every RNG state"]
            immune = _immune_note(move, enemy_mon, ctx["chart"])
            if immune:
                notes.insert(0, immune)
            out.append(_entry_exact(ctx, move_id, _exact(
                "seed-independent", probability={"num": bound, "den": 1},
                ko=(immune if immune else
                    ("damage >= remaining HP of enemy active" if bound
                     else "max-roll damage < remaining HP of enemy active")),
                notes=notes)))
            continue
        rng = Rng(snap["rng"]["state"])
        events, post, _ = _play_turn(_state(ctx), ("move", move), rng, ctx)
        ko = post["enemy"]["party"][ctx["e_index"]]["hp"] <= 0
        notes = ["seed fixed every draw in the turn"]
        if any(e["kind"] == "miss" for e in events):
            notes.append("the move missed on this seed")
        if any(e["kind"] == "immune" for e in events):
            notes.append("the target is immune to this move")
        out.append(_entry_exact(ctx, move_id, _exact(
            "known-seed", probability={"num": 1 if ko else 0, "den": 1},
            ko=("damage >= remaining HP of enemy active" if ko
                else "damage < remaining HP of enemy active"), notes=notes)))
    return _entry_card(out, "moves")


def _enemy_moves_card(snap, shared, base, clock):
    """P(KO of the player active | each enemy move), conditioned on the plan."""
    gate = base.copy()
    player_party, enemy_party = _active_pair(snap, shared, gate)
    ctx = _ctx(shared, player_party, enemy_party)
    player_mon, enemy_mon, failure = _require_actives(gate, ctx)
    if failure is not None:
        return failure
    player_action, action_failure = _planned_action(snap, ctx)
    if player_action is None:
        return _unavailable(*action_failure)
    entries = _move_entries(enemy_mon)
    if not entries:
        return _unavailable("no-legal-actions",
                            "the enemy's active Pokémon has no selectable move: every "
                            "slot is empty or at 0 PP, so nothing can be selected")
    # With an entered seed, the AI's preselection can consume RNG before the
    # conditioned move. A seed-independent bound does not read those draws.
    if snap["rng"]["state"] is not None:
        bench_failure = _action_gate(base, shared).result()
        if bench_failure is not None:
            return _unavailable(*bench_failure)
    if player_action[0] == "switch":
        blockers = [("switch", player_action[1], None)]
    else:
        blockers = [("move", player_action[1], player_mon)]
    out = []
    for move_id, move, move_failure in entries:
        if move is None:
            out.append(_entry_failure(ctx, move_id, move_failure,
                                      ["conditioned on the entered planned player action"]))
            continue
        if clock.expired():
            return _unavailable("timeout", "deadline passed while evaluating enemy moves")
        if snap["rng"]["state"] is None:
            bound = _ko_bound(move, enemy_mon, player_mon, blockers, ctx)
            if bound is None:
                out.append(_entry_failure(ctx, move_id, (
                    "unknown-rng-seed",
                    "the knockout depends on this turn's RNG draws and no seed was entered",
                    [], []), ["conditioned on the entered planned player action"]))
                continue
            notes = ["conditioned on the entered planned player action",
                     "the outcome is identical for every RNG state"]
            immune = _immune_note(move, player_mon, ctx["chart"])
            if immune:
                notes.insert(1, immune)
            out.append(_entry_exact(ctx, move_id, _exact(
                "seed-independent", probability={"num": bound, "den": 1},
                ko=(immune if immune else
                    ("damage >= remaining HP of player active" if bound
                     else "max-roll damage < remaining HP of player active")),
                notes=notes)))
            continue
        rng = Rng(snap["rng"]["state"])
        events, post, _ = _play_turn(_state(ctx), player_action, rng, ctx,
                                     forced_enemy_move=move)
        ko = post["player"]["party"][ctx["p_index"]]["hp"] <= 0
        notes = ["conditioned on the entered planned player action",
                 "seed fixed every draw in the turn"]
        if any(e["kind"] == "miss" for e in events):
            notes.append("the move missed on this seed")
        if any(e["kind"] == "immune" for e in events):
            notes.append("the target is immune to this move")
        out.append(_entry_exact(ctx, move_id, _exact(
            "known-seed", probability={"num": 1 if ko else 0, "den": 1},
            ko=("damage >= remaining HP of player active" if ko
                else "damage < remaining HP of player active"), notes=notes)))
    return _entry_card(out, "moves")


def _action_entry(ctx, kind, action_id, label=None):
    return {"kind": kind, "id": action_id,
            "label": label if label is not None else action_id,
            "probability": {"num": 1, "den": 1}, "top": True}


def _ai_actions_card(snap, shared, base, clock):
    """Distribution over the AI's legal actions this turn (sums to exactly 1).

    Reads the enemy active's legal moves, the player active (AI damage estimates
    and speed/priority) and the trainer's bag items -- nothing else.
    """
    gate = _action_gate(base, shared)
    player_party, enemy_party = _active_pair(snap, shared, gate)
    ctx = _ctx(shared, player_party, enemy_party)
    player_mon, enemy_mon, failure = _require_actives(gate, ctx)
    if failure is not None:
        return failure
    if enemy_mon["unsupported_moves"]:
        return _unavailable(*_unsupported_failure(
            "enemy.party[%d]" % ctx["e_index"], enemy_mon,
            "the enemy's active Pokémon has moves this engine has not ported, so its "
            "action cannot be determined"))
    if ctx["items"] and player_mon["unsupported_moves"]:
        # ShouldUseItem -> AiExpectsToFaintPlayer -> CanTargetFaintAiWithMod walks
        # the player's moves (src/battle_ai_util.c:1427), so an unported player move
        # makes the item decision unknown.
        return _unavailable(*_unsupported_failure(
            "player.party[%d]" % ctx["p_index"], player_mon,
            "the player's active Pokémon has moves this engine has not ported, so the "
            "AI's item decision cannot be determined"))
    tied = _ai_max_score_moves(enemy_mon, player_mon, ctx)
    if not tied or tied[0][1] <= 0:
        # A cleared slot is the only thing that removes a move here: the immunity
        # penalty is RETURN_SCORE_MINUS(20) (src/battle_ai_main.c:779) which leaves
        # 80 > 0, and IsDamageMoveUnusable (src/battle_ai_util.c:446) only feeds the
        # damage estimate inside AI_CalcDamage (called at :757), never the choice.
        return _unavailable("no-legal-actions",
                            "the enemy's active Pokémon has no selectable move: every "
                            "slot is empty or at 0 PP, so nothing can be selected")
    if snap["rng"]["state"] is None:
        if len(tied) == 1:
            move = tied[0][0]
            item = _ai_should_use_item(enemy_mon, player_mon, ctx, move, enemy_party)
            if item is not None:
                return _exact("seed-independent",
                              actions=[_action_entry(ctx, "item", item[0])],
                              conditioned_on="entered battle state")
            return _exact("seed-independent",
                          actions=[_action_entry(ctx, "move", move["id"],
                                                 _move_name(ctx, move["id"]))],
                          conditioned_on="entered battle state")
        return _unavailable("unknown-rng-seed",
                            "the AI tie-break draw needs a seed when several moves tie")
    rng = Rng(snap["rng"]["state"])
    state = _state(ctx)
    action = _choose_enemy_action(state, rng, ctx)
    if action is None:
        return _unavailable("no-legal-actions", "the AI has no usable action")
    if action[0] == "item":
        return _exact("known-seed", actions=[_action_entry(ctx, "item", action[1][0])],
                      conditioned_on="entered battle state")
    return _exact("known-seed",
                  actions=[_action_entry(ctx, "move", action[1]["id"],
                                         _move_name(ctx, action[1]["id"]))],
                  conditioned_on="entered battle state")


def _candidate_actions(state, ctx):
    out = []
    player = state["player"]
    active = player["party"][player["active"]]
    for move in _usable_moves(active, ctx):
        out.append((move["id"], ("move", move)))
    for index, member in enumerate(player["party"]):
        if index == player["active"] or member is None:
            continue
        if member.get("fainted") or not (member.get("revived") or member.get("hp", 0) > 0):
            continue
        out.append(("switch:%d" % index, ("switch", index)))
    return out


def _terminal(state):
    return (_living_count(state["player"]["party"]) == 0
            or _living_count(state["enemy"]["party"]) == 0)


def _player_bench_for_recommendation(snap, gd, p_index):
    """Resolve the player's live bench members (legal switch candidates).

    Returns ``(members, failures)``.  ``members[index]`` is the resolved member
    (a fainted slot resolves to the fainted shape and is not a candidate) and
    ``failures`` lists the legal actions this engine cannot evaluate -- with a
    strict contract rule, one of those makes the whole recommendation unavailable.
    """
    members = [None] * len(snap["player"]["party"])
    failures = []
    for index, build in enumerate(snap["player"]["party"]):
        if index == p_index or build["species"] in (None, "SPECIES_NONE"):
            continue
        if build["hp"] == 0:
            members[index] = _fainted_member(build["species"], build)
            continue
        if build["hp"] is None:
            failures.append((index, ("missing-input",
                                     "the switch candidate's HP is unknown",
                                     ["player.party[%d].hp" % index], [])))
            continue
        local = _Gate()
        member = _resolve_member(build, gd, local, "player.party[%d]" % index)
        if member is None:
            failures.append((index, local.result()))
        else:
            members[index] = member
    return members, failures


def _recommendation_card(snap, shared, base, clock):
    """Two-turn lookahead maximising the contract section 3 score.

    Strict: every legal first-turn player action is a reachable path, so an action
    this engine cannot evaluate makes the whole card unavailable rather than
    silently shrinking the maximum.
    """
    if snap["rng"]["state"] is None:
        return _unavailable("unknown-rng-seed",
                            "the two-turn lookahead needs an entered RNG seed")
    gate = _action_gate(base, shared)
    player_party, enemy_party = _active_pair(snap, shared, gate, bench=("skip", "score"))
    bench, failures = _player_bench_for_recommendation(snap, shared["gd"], shared["p_index"])
    for index, member in enumerate(bench):
        if member is not None:
            player_party[index] = member
    ctx = _ctx(shared, player_party, enemy_party)
    if failures:
        index, result = failures[0]
        return _unavailable("incomplete-search",
                            "player action switch:%d cannot be evaluated: %s"
                            % (index, result[1]),
                            missing=result[2],
                            mechanics=list(result[3]) + ["switch:%d" % index])
    player_mon, enemy_mon, failure = _require_actives(gate, ctx)
    if failure is not None:
        return failure
    if player_mon["unsupported_moves"]:
        # contract section 3: every legal first-turn player action is a reachable
        # path, so an action that cannot be evaluated makes the maximum unknown
        failure = _unsupported_failure(
            "player.party[%d]" % ctx["p_index"], player_mon,
            "the player's active Pokémon has moves this engine has not ported, so the "
            "two-turn maximum is unknown")
        return _unavailable("incomplete-search", failure[1], failure[2], failure[3])
    if enemy_mon["unsupported_moves"]:
        failure = _unsupported_failure(
            "enemy.party[%d]" % ctx["e_index"], enemy_mon,
            "the enemy's active Pokémon has moves this engine has not ported, so the "
            "two-turn maximum is unknown")
        return _unavailable("incomplete-search", failure[1], failure[2], failure[3])
    state = _state(ctx)
    actions = _candidate_actions(state, ctx)
    if not actions:
        return _unavailable("no-legal-actions", "the player has no legal action")
    seed = snap["rng"]["state"]
    results = []
    for label, action in actions:
        if clock.expired():
            return _unavailable("timeout", "deadline passed during the two-turn lookahead")
        rng = Rng(seed)
        _, post, _ = _play_turn(state, action, rng, ctx)
        if _terminal(post) or _terminal(state):
            results.append((label, action, _score(post)))
            continue
        best = None
        for _, second in _candidate_actions(post, ctx):
            if clock.expired():
                return _unavailable("timeout", "deadline passed during the two-turn lookahead")
            rng2 = Rng(rng.snapshot())
            _, post2, _ = _play_turn(post, second, rng2, ctx)
            value = _score(post2)
            if best is None or value > best:
                best = value
        results.append((label, action, best if best is not None else _score(post)))
    top = max(score for _, _, score in results)
    winners = [(label, action, score) for label, action, score in results if score == top]
    breakdown = ["%s -> %d/%d" % (label, score.numerator, score.denominator)
                 for label, _, score in results]
    excluded = [index for index, build in enumerate(snap["enemy"]["party"])
                if index != ctx["e_index"] and build["species"] not in (None, "SPECIES_NONE")
                and state["enemy"]["party"][index] is None]
    notes = []
    if excluded:
        notes.append("%d enemy party member(s) excluded from the score: no HP entered "
                     "and the switch decision is provably FALSE, so they cannot be "
                     "reached inside the two-turn window" % len(excluded))
    return _exact("known-seed", label="two-turn lookahead", scored_turns=2,
                  actions=[{"kind": action[0],
                            "id": action[1]["id"] if action[0] == "move" else "slot:%d" % action[1],
                            "label": label,
                            "score": _fraction(score)}
                           for label, action, score in winners],
                  breakdown=breakdown, notes=notes)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

class _Clock(object):
    """Deadline check.  ``time.monotonic`` is only imported when a deadline exists."""

    __slots__ = ("_deadline", "_monotonic")

    def __init__(self, deadline):
        self._deadline = deadline
        if deadline is None:
            self._monotonic = None
        else:
            import time
            self._monotonic = time.monotonic

    def expired(self):
        return self._deadline is not None and self._monotonic() > self._deadline


def evaluate_head_to_head(snapshot, game_data, deadline=None):
    """Evaluate the four Calc cards.  Pure, JSON-serialisable, no IO.

    Each card is prepared and gated on its own (contract section 3); a card is
    only unavailable because of an input or mechanic its own computation reads.
    """
    clock = _Clock(deadline)
    if clock.expired():
        return _cards_unavailable("timeout",
                                  "the deadline had already passed before evaluation began")
    try:
        snap = normalize_snapshot(snapshot)
    except ValueError as exc:
        return _cards_unavailable("engine-error", "invalid snapshot: %s" % exc)
    try:
        shared, base = _prepare(snap, game_data)
        if shared is None:
            return _cards_unavailable(*base)
        cards = (
            _player_moves_card(snap, shared, base, clock),
            _enemy_moves_card(snap, shared, base, clock),
            _ai_actions_card(snap, shared, base, clock),
            _recommendation_card(snap, shared, base, clock),
        )
    except Exception as exc:                                 # pragma: no cover - defensive
        return _cards_unavailable("engine-error", "engine failed: %r" % exc)
    return {
        "status": "exact" if all(c["status"] == "exact" for c in cards) else "unavailable",
        "source_sha": SOURCE_SHA,
        "engine_version": ENGINE_VERSION,
        "evidence": dict(EVIDENCE_BASE),
        "player_moves": cards[0],
        "enemy_moves": cards[1],
        "ai_actions": cards[2],
        "recommendation": cards[3],
    }


# ---------------------------------------------------------------------------
# self-check
# ---------------------------------------------------------------------------

def _demo_snapshot():
    return {
        "schema_version": 1,
        "source_sha": SOURCE_SHA,
        "difficulty": "Normal",
        "progress": {"milestone_id": "CAP_1", "flags": {}},
        "player": {
            "party": [{
                "slot_id": 1, "species": "SPECIES_PIKACHU", "level": 25,
                "ability": NO_ABILITY, "item": NO_ITEM, "nature": "NATURE_SERIOUS",
                "ivs": {s: 31 for s in STATS}, "evs": {s: 0 for s in STATS},
                "moves": ["MOVE_THUNDERBOLT", "MOVE_QUICK_ATTACK"],
                "pp": {"MOVE_THUNDERBOLT": 15, "MOVE_QUICK_ATTACK": 30},
                "max_hp": 70, "hp": 70, "status": "none", "stat_stages": {},
                "use_item": False,
            }],
            "active_index": 0,
        },
        "enemy": {
            "trainer_id": "TRAINER_DEMO",
            "party": [{
                "slot_id": None, "species": "SPECIES_SQUIRTLE", "level": 22,
                "ability": NO_ABILITY, "item": NO_ITEM, "nature": "NATURE_SERIOUS",
                "ivs": {s: 31 for s in STATS}, "evs": {s: 0 for s in STATS},
                "moves": ["MOVE_WATER_GUN"], "pp": {"MOVE_WATER_GUN": 25},
                "max_hp": 60, "hp": 60, "status": "none", "stat_stages": {},
                "use_item": False,
            }],
            "active_index": 0, "items": {}, "ai_flags": [],
        },
        "field": {key: None for key in _FIELD_KEYS},
        "rng": {"state": [0x12345678, 0x9ABCDEF0, 0x0BADF00D, 0x1]},
        "history": {"turns": []},
        "planned_action": {"kind": "move", "move_id": "MOVE_THUNDERBOLT",
                           "target_index": None},
    }


def _demo_game_data():
    def stats(hp, atk, dfn, spa, spd, spe):
        return {"hp": hp, "attack": atk, "defense": dfn, "sp_attack": spa,
                "sp_defense": spd, "speed": spe}

    types = ["TYPE_NORMAL", "TYPE_ELECTRIC", "TYPE_WATER", "TYPE_GROUND"]
    chart = {atk: {dfn: 1.0 for dfn in types} for atk in types}
    chart["TYPE_ELECTRIC"]["TYPE_WATER"] = 2.0
    chart["TYPE_ELECTRIC"]["TYPE_GROUND"] = 0.0
    return {
        "schema_version": 2,
        "species": {
            "SPECIES_PIKACHU": {"name": "Pikachu", "types": ["TYPE_ELECTRIC"],
                                "stats": stats(35, 55, 40, 50, 50, 90)},
            "SPECIES_SQUIRTLE": {"name": "Squirtle", "types": ["TYPE_WATER"],
                                 "stats": stats(44, 48, 65, 50, 64, 43)},
        },
        "type_chart": chart,
        "trainers": [{"id": "TRAINER_DEMO", "is_boss": False,
                      "conditional_party": False, "items": [], "party": []}],
        "moves": {
            "MOVE_THUNDERBOLT": {"name": "Thunderbolt", "type": "TYPE_ELECTRIC",
                                 "power": 90, "accuracy": 100, "pp": 15,
                                 "category": "special", "effect": "EFFECT_HIT",
                                 "priority": 0, "always_critical": False,
                                 "crit_ratio": 0, "strike_count": 1, "recoil": 0,
                                 "flags": [], "secondary_effects": []},
            "MOVE_QUICK_ATTACK": {"name": "Quick Attack", "type": "TYPE_NORMAL",
                                  "power": 40, "accuracy": 100, "pp": 30,
                                  "category": "physical", "effect": "EFFECT_HIT",
                                  "priority": 1, "always_critical": False,
                                  "crit_ratio": 0, "strike_count": 1, "recoil": 0,
                                  "flags": ["makesContact"], "secondary_effects": []},
            "MOVE_WATER_GUN": {"name": "Water Gun", "type": "TYPE_WATER",
                               "power": 40, "accuracy": 100, "pp": 25,
                               "category": "special", "effect": "EFFECT_HIT",
                               "priority": 0, "always_critical": False,
                               "crit_ratio": 0, "strike_count": 1, "recoil": 0,
                               "flags": [], "secondary_effects": []},
        },
        "oracle": {"available": False, "reason": "no local emulator oracle"},
    }


if __name__ == "__main__":
    import json
    result = evaluate_head_to_head(_demo_snapshot(), _demo_game_data())
    print(json.dumps(result, indent=2, sort_keys=True))
