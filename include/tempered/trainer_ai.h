#ifndef GUARD_TEMPERED_TRAINER_AI_H
#define GUARD_TEMPERED_TRAINER_AI_H

// Extra AI flags added on top of a trainer's own flags in regular trainer battles.
u64 Tempered_GetExtraTrainerAiFlags(u16 trainerId);

// Whether every trainer in this region map section uses Smart Trainer AI.
bool32 Tempered_IsSmartTrainerZone(mapsec_u8_t mapSec);

#endif // GUARD_TEMPERED_TRAINER_AI_H
