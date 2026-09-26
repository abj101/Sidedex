#include "global.h"
#include "test/battle.h"

AI_SINGLE_BATTLE_TEST("Sidedex oracle: deterministic smart AI trace")
{
    GIVEN {
        RNGSeed(((rng_value_t){ .a = 0x12345678, .b = 0x9ABCDEF0, .c = 0x0BADF00D, .ctr = 1 }));
        AI_FLAGS(AI_FLAG_CHECK_BAD_MOVE | AI_FLAG_CHECK_VIABILITY | AI_FLAG_TRY_TO_FAINT | AI_FLAG_SMART_SWITCHING);
        PLAYER(SPECIES_BULBASAUR) { Level(50); Speed(150); Moves(MOVE_WILL_O_WISP); }
        OPPONENT(SPECIES_CHARMANDER) { Level(50); Speed(100); Ability(ABILITY_INTIMIDATE); Moves(MOVE_TACKLE); }
        OPPONENT(SPECIES_HEATRAN) { Level(50); Speed(80); Ability(ABILITY_FLASH_FIRE); Moves(MOVE_TACKLE); }
    } WHEN {
        TURN { MOVE(player, MOVE_WILL_O_WISP, hit: TRUE); EXPECT_MOVE(opponent, MOVE_TACKLE); }
    } SCENE {
    }
}
