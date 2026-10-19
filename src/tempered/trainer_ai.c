#include "global.h"
#include "data.h"
#include "overworld.h"
#include "tempered/trainer_ai.h"
#include "constants/battle_ai.h"
#include "constants/region_map_sections.h"
#include "constants/trainers.h"

// Expert difficulty: bosses, strong regular trainers and every trainer in a smart zone use
// expansion's Smart Trainer AI; everyone else uses Basic Trainer AI.
// In-game partners (e.g. Steven) follow the same rules. See TEMPERED.md.
static bool32 IsSmartTrainerClass(enum TrainerClassID trainerClass)
{
    switch (trainerClass)
    {
    case TRAINER_CLASS_LEADER:
    case TRAINER_CLASS_ELITE_FOUR:
    case TRAINER_CLASS_CHAMPION:
    case TRAINER_CLASS_RIVAL:
    case TRAINER_CLASS_AQUA_LEADER:
    case TRAINER_CLASS_MAGMA_LEADER:
    case TRAINER_CLASS_AQUA_ADMIN:
    case TRAINER_CLASS_MAGMA_ADMIN:
    case TRAINER_CLASS_COOLTRAINER:
    case TRAINER_CLASS_COOLTRAINER_2:
    case TRAINER_CLASS_EXPERT:
    case TRAINER_CLASS_DRAGON_TAMER:
    case TRAINER_CLASS_OLD_COUPLE:
    case TRAINER_CLASS_WINSTRATE:
    case TRAINER_CLASS_PKMN_RANGER:
        return TRUE;
    default:
        return FALSE;
    }
}

// Areas where every trainer uses Smart Trainer AI, whatever their class.
static const mapsec_u8_t sSmartTrainerZones[] =
{
    MAPSEC_VICTORY_ROAD,
};

bool32 Tempered_IsSmartTrainerZone(mapsec_u8_t mapSec)
{
    for (u32 i = 0; i < ARRAY_COUNT(sSmartTrainerZones); i++)
    {
        if (sSmartTrainerZones[i] == mapSec)
            return TRUE;
    }
    return FALSE;
}

u64 Tempered_GetExtraTrainerAiFlags(u16 trainerId)
{
    if (IsSmartTrainerClass(GetTrainerClassFromId(trainerId))
     || Tempered_IsSmartTrainerZone(GetCurrentRegionMapSectionId()))
        return AI_FLAG_SMART_TRAINER;

    return AI_FLAG_BASIC_TRAINER;
}
