#include "global.h"
#include "data.h"
#include "overworld.h"
#include "tempered/trainer_ai.h"
#include "constants/battle_ai.h"
#include "constants/region_map_sections.h"
#include "test/test.h"

// IDs from test/battle/trainer_control.party and test/battle/partner_control.party.
#define TEST_TRAINER_RIVAL      1
#define TEST_TRAINER_BLACK_BELT 14
#define PARTNER_STEVEN_TEST     1

TEST("Tempered trainer AI: smart trainer classes get Smart Trainer AI")
{
    ASSUME(GetTrainerClassFromId(TEST_TRAINER_RIVAL) == TRAINER_CLASS_RIVAL);
    EXPECT(Tempered_GetExtraTrainerAiFlags(TEST_TRAINER_RIVAL) == AI_FLAG_SMART_TRAINER);
}

TEST("Tempered trainer AI: other trainer classes get Basic Trainer AI")
{
    ASSUME(GetTrainerClassFromId(TEST_TRAINER_BLACK_BELT) == TRAINER_CLASS_BLACK_BELT);
    EXPECT(Tempered_GetExtraTrainerAiFlags(TEST_TRAINER_BLACK_BELT) == AI_FLAG_BASIC_TRAINER);
}

TEST("Tempered trainer AI: in-game partners follow the trainer class rule")
{
    ASSUME(GetTrainerClassFromId(TRAINER_PARTNER(PARTNER_STEVEN_TEST)) == TRAINER_CLASS_RIVAL);
    EXPECT(Tempered_GetExtraTrainerAiFlags(TRAINER_PARTNER(PARTNER_STEVEN_TEST)) == AI_FLAG_SMART_TRAINER);
}

TEST("Tempered trainer AI: Victory Road is a smart trainer zone")
{
    EXPECT(Tempered_IsSmartTrainerZone(MAPSEC_VICTORY_ROAD));
}

TEST("Tempered trainer AI: other areas are not smart trainer zones")
{
    EXPECT(!Tempered_IsSmartTrainerZone(MAPSEC_ROUTE_101));
}

TEST("Tempered trainer AI: trainers in a smart zone get Smart Trainer AI whatever their class")
{
    struct WarpData savedLocation = gSaveBlock1Ptr->location;

    gSaveBlock1Ptr->location.mapGroup = MAP_GROUP(MAP_VICTORY_ROAD_1F);
    gSaveBlock1Ptr->location.mapNum = MAP_NUM(MAP_VICTORY_ROAD_1F);
    mapsec_u8_t mapSec = GetCurrentRegionMapSectionId();
    u64 flags = Tempered_GetExtraTrainerAiFlags(TEST_TRAINER_BLACK_BELT);
    gSaveBlock1Ptr->location = savedLocation;

    ASSUME(GetTrainerClassFromId(TEST_TRAINER_BLACK_BELT) == TRAINER_CLASS_BLACK_BELT);
    ASSUME(mapSec == MAPSEC_VICTORY_ROAD);
    EXPECT(flags == AI_FLAG_SMART_TRAINER);
}
