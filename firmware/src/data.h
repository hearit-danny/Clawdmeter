#pragma once
#include <Arduino.h>

#define MACHINE_MAX_RIGS 8

enum rig_state_t : uint8_t { RIG_DOWN = 0, RIG_UP = 1, RIG_PARTIAL = 2 };

struct RigStatus {
    char    name[10];        // short rig name, e.g. "inv1"
    uint8_t state;           // rig_state_t: all ports / some ports / none listening
};

// Machine meter keys ("cpu", "rf", "rp", "rg"). The daemon sends them only when
// a value changed, so they persist across payloads that omit them.
struct MachineData {
    uint32_t seq;            // bumped on every payload that carried them; 0 = never
    int      cpu_pct;        // 0-100, all cores
    float    ram_free_gb;    // one decimal
    int      ram_used_pct;   // 0-100
    uint8_t  rig_count;
    RigStatus rigs[MACHINE_MAX_RIGS];   // up first, then partial, then down
};

struct UsageData {
    float session_pct;       // utilization 0-100 (5h window Pro/Max; spending % Enterprise)
    int session_reset_mins;  // minutes until reset
    float weekly_pct;        // 7-day utilization (Pro/Max only; 0 for Enterprise)
    float surplus_pct;       // 7-day rolling weekly budget
    bool has_surplus;        // daemon sent "sp"; false hides the pace label
    int weekly_reset_mins;   // minutes until weekly reset (Pro/Max only)
    char status[16];         // "allowed", "limited", etc.
    bool chime;              // play the session-reset chime; false unless daemon opts in
    bool enterprise;         // true = Enterprise spending-limit account
    int time_pct;            // 0-100: fraction of billing period elapsed (Enterprise)
    int period_days;         // total billing period length in days (Enterprise)
    char reset_date[12];     // formatted reset date e.g. "Jul 1" (Enterprise)
    long clock_epoch;        // local wall-clock epoch (s) from daemon; 0 = not provided
    int  clock_fmt;          // 12 or 24 (hour format from daemon); defaults to 24
    bool ok;                 // data parse succeeded
    bool valid;              // false until first successful parse
    MachineData machine;     // optional machine meter; old daemons never send it
};
