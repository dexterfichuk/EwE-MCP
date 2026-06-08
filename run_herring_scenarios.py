#!/usr/bin/env python3
"""
Comprehensive Herring Improvement Scenarios for Model V5.

Runs 7 scenarios on Model V5, records herring J(62) and A(63) biomass,
and saves results to JSON.

Scenarios:
  1. Baseline - scenario 1 as-is
  2. Reduce Herring Fishing Mortality (Fleet 9 pcap_base: 50%)
  3. Reduce Marine Mammal Predation (vul_mult: Steller sea lions, porpoises, seals -> 2.0)
  4. Reduce Salmon Predation (vul_mult: FRGSPS SP returning spawners -> 2.0)
  5. Increase Zooplankton Production (max_rel_pb: 2.0 -> 5.0)
  6. Groundfish Predation Reduction (vul_mult: halibut, hake, rockfish, gadids, lingcod -> 1.0)
  7. Combined: fishing 50% + mammal vuln -> 2.0 + zooplankton production
"""
import json
import sys
import time
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from ewe_mcp.client import EwEMCPClient


# ── Configuration ──────────────────────────────────────────────────────────────
MODEL_PATH = r"C:\Users\dexter\Downloads\Model V5.eweaccdb"
OUTPUT_PATH = "/tmp/herring_scenarios_results.json"

GROUP_HERRING_J = 62
GROUP_HERRING_A = 63
GROUP_ZOOPLANKTON = 73  # Both herring stages eat this primarily
FLEET_HERRING = 9

# Key predator groups for herring A
PRED_MAMMALS = [4, 5, 6]  # Porpoises/dolphins, Harbor seals, Steller sea lions
PRED_SALMON_HIGH = [13]   # FRGSPS SP returning spawners (vul_mult=100M)
PRED_GROUNDFISH = [64, 65, 66, 67, 68]  # Halibut, Hake, Rockfish, Gadids, Lingcod


def run_ecosim_and_wait(client, timeout_sec=120, poll_interval=2):
    """Run ecosim and wait for completion."""
    r = client.command_result("run_ecosim", {})
    if not r.get("started", False):
        raise RuntimeError(f"Failed to start Ecosim: {r}")
    
    for step in range(timeout_sec // poll_interval):
        time.sleep(poll_interval)
        rs = client.command_result("get_ecosim_run_state", {})
        if not rs.get("is_busy", True) and rs.get("has_ecosim_ran", False):
            return
    raise TimeoutError("Ecosim did not complete within timeout")


def get_herring_results(client):
    """Extract herring J and A biomass from latest run."""
    r = client.command_result("get_ecosim_group_results", {"list": True})
    results = {}
    for gr in r.get("groups", []):
        if gr["index"] in (GROUP_HERRING_J, GROUP_HERRING_A):
            s = gr["summary"]
            name = "herring_j" if gr["index"] == GROUP_HERRING_J else "herring_a"
            bs = s.get("biomass_start", 0)
            be = s.get("biomass_end", 0)
            results[name] = {
                "start": round(bs, 4),
                "end": round(be, 4),
            }
            if bs > 0:
                results[name]["change_pct"] = round(
                    (be - bs) / bs * 100, 1
                )
            else:
                results[name]["change_pct"] = 0.0
    return results


def init_model(client):
    """Load model and scenario 1, run ecopath if needed."""
    client.command_result("close_model", {})
    r = client.command_result("load_model", {"path": MODEL_PATH}, timeout=60)
    if not r.get("loaded", False):
        raise RuntimeError(f"Failed to load model: {r}")
    
    r = client.command_result("load_ecosim_scenario", {"index": 1})
    if not r.get("loaded", False):
        raise RuntimeError(f"Failed to load scenario 1: {r}")
    
    rs = client.command_result("get_ecopath_run_state", {})
    if not rs.get("has_ecopath_ran", False):
        r = client.command_result("run_ecopath", {})
        print(f"  Ran Ecopath: {r}")
    
    return True


def read_all_original_values(client):
    """Read all original parameter values for later restoration."""
    values = {}
    
    # Fleet 9 pcap_base
    r = client.command_result("get_ecosim_fleet_input", {"index": FLEET_HERRING})
    values["fleet9_pcap_base"] = r.get("values", {}).get("pcap_base")
    
    # Herring A vul_mult to all predators
    values["herringA_vul_mult"] = {}
    for pred in range(1, 76):
        r = client.command_result("get_indexed_value", {
            "domain": "ecosim_group_inputs",
            "index": GROUP_HERRING_A,
            "variable": "vul_mult",
            "indices": [pred],
            "navigate": False,
        })
        val = r.get("value")
        if val is not None:
            values["herringA_vul_mult"][pred] = val
    
    # Herring J vul_mult to all predators
    values["herringJ_vul_mult"] = {}
    for pred in range(1, 76):
        r = client.command_result("get_indexed_value", {
            "domain": "ecosim_group_inputs",
            "index": GROUP_HERRING_J,
            "variable": "vul_mult",
            "indices": [pred],
            "navigate": False,
        })
        val = r.get("value")
        if val is not None:
            values["herringJ_vul_mult"][pred] = val
    
    # Zooplankton max_rel_pb
    r = client.command_result("get_ecosim_group_input", {"index": GROUP_ZOOPLANKTON})
    values["zooplankton_max_rel_pb"] = r.get("values", {}).get("max_rel_pb")
    
    return values


def restore_all_values(client, orig):
    """Restore all original values carefully."""
    # Fleet 9 pcap_base
    r = client.command_result("get_ecosim_fleet_input", {"index": FLEET_HERRING})
    if r.get("values", {}).get("pcap_base") is not None:
        current = r["values"]["pcap_base"]
        target = orig["fleet9_pcap_base"]
        if abs(current - target) > 0.001:
            client.command_result("set_values", {
                "domain": "ecosim_fleet_inputs",
                "index": FLEET_HERRING,
                "values": {"pcap_base": target},
                "navigate": False,
            })
    
    # Herring A vul_mult
    for pred, val in orig["herringA_vul_mult"].items():
        r = client.command_result("get_indexed_value", {
            "domain": "ecosim_group_inputs",
            "index": GROUP_HERRING_A,
            "variable": "vul_mult",
            "indices": [pred],
            "navigate": False,
        })
        current = r.get("value")
        if current is not None and abs(current - val) > 0.001:
            client.command_result("set_indexed_value", {
                "domain": "ecosim_group_inputs",
                "index": GROUP_HERRING_A,
                "variable": "vul_mult",
                "indices": [pred],
                "value": val,
                "navigate": False,
            })
    
    # Herring J vul_mult
    for pred, val in orig["herringJ_vul_mult"].items():
        r = client.command_result("get_indexed_value", {
            "domain": "ecosim_group_inputs",
            "index": GROUP_HERRING_J,
            "variable": "vul_mult",
            "indices": [pred],
            "navigate": False,
        })
        current = r.get("value")
        if current is not None and abs(current - val) > 0.001:
            client.command_result("set_indexed_value", {
                "domain": "ecosim_group_inputs",
                "index": GROUP_HERRING_J,
                "variable": "vul_mult",
                "indices": [pred],
                "value": val,
                "navigate": False,
            })
    
    # Zooplankton max_rel_pb
    r = client.command_result("get_ecosim_group_input", {"index": GROUP_ZOOPLANKTON})
    if r.get("values", {}).get("max_rel_pb") is not None:
        current = r["values"]["max_rel_pb"]
        target = orig["zooplankton_max_rel_pb"]
        if abs(current - target) > 0.001:
            client.command_result("set_values", {
                "domain": "ecosim_group_inputs",
                "index": GROUP_ZOOPLANKTON,
                "values": {"max_rel_pb": target},
                "navigate": False,
            })
    
    # Discard changes flag
    client.command_result("discard_changes", {})


def run_scenario(client, orig, name, scenario_type, changes, modifications_fn):
    """Run a single scenario with modifications."""
    print(f"\n{'='*60}")
    print(f"Scenario: {name}")
    print(f"{'='*60}")
    
    # Apply modifications
    modifications_fn(client)
    
    # Run ecosim
    print("  Running Ecosim...")
    try:
        run_ecosim_and_wait(client)
    except (RuntimeError, TimeoutError) as e:
        print(f"  ERROR: {e}")
        return None
    
    # Extract results
    herring = get_herring_results(client)
    hj = herring.get("herring_j", {})
    ha = herring.get("herring_a", {})
    print(f"  Herring J: start={hj.get('start')}, end={hj.get('end')}, change={hj.get('change_pct')}%")
    print(f"  Herring A: start={ha.get('start')}, end={ha.get('end')}, change={ha.get('change_pct')}%")
    
    info = client.command_result("get_ecosim_run_state", {})
    
    return {
        "name": name,
        "type": scenario_type,
        "changes": changes,
        "herring_j": herring.get("herring_j", {"start": 0, "end": 0, "change_pct": 0}),
        "herring_a": herring.get("herring_a", {"start": 0, "end": 0, "change_pct": 0}),
        "results": {
            "n_groups": 75,
            "n_fleets": 10,
            "years": info.get("years", 42),
        },
    }


def main():
    client = EwEMCPClient()
    client.connect()
    
    try:
        # ── Step 1: Init model ───────────────────────────────────
        print("Step 1: Loading Model V5 and scenario 1...")
        init_model(client)
        print("  Model loaded, scenario 1 ready")
        
        # ── Step 2: Read all original values ─────────────────────
        print("\nStep 2: Reading original parameter values...")
        orig = read_all_original_values(client)
        print(f"  Fleet 9 pcap_base: {orig['fleet9_pcap_base']}")
        print(f"  Herring A high vul_mult (pred -> value):")
        for pred, val in sorted(orig["herringA_vul_mult"].items()):
            if val > 2.0:
                print(f"    predator {pred}: {val}")
        print(f"  Zooplankton max_rel_pb: {orig['zooplankton_max_rel_pb']}")
        
        # ── Step 3: Run scenarios ────────────────────────────────
        scenarios = []
        
        # Scenario 1: Baseline
        s1 = run_scenario(
            client, orig,
            name="Baseline",
            scenario_type="baseline",
            changes={},
            modifications_fn=lambda cl: None,
        )
        if s1:
            scenarios.append(s1)
        
        # ── Scenario 2: Reduce Herring Fishing Mortality ──
        new_pcap = orig["fleet9_pcap_base"] * 0.5
        row2_changes = {"fleet9_pcap_base": {"from": orig["fleet9_pcap_base"], "to": new_pcap}}
        
        def mod_fishing(cl, target=new_pcap):
            cl.command_result("set_values", {
                "domain": "ecosim_fleet_inputs", "index": FLEET_HERRING,
                "values": {"pcap_base": target}, "navigate": False,
            })
        
        s2 = run_scenario(client, orig,
            name="Reduce Herring Fishing (50%)",
            scenario_type="fishing_reduction",
            changes=row2_changes,
            modifications_fn=mod_fishing,
        )
        if s2:
            scenarios.append(s2)
        restore_all_values(client, orig)
        
        # ── Scenario 3: Reduce Marine Mammal Predation ──
        s3_changes = {
            "vul_mult_to_mammals": {
                "predators": PRED_MAMMALS,
                "action": "set to 2.0 (from extreme values)",
            }
        }
        
        def mod_mammals(cl):
            for pred in PRED_MAMMALS:
                cl.command_result("set_indexed_value", {
                    "domain": "ecosim_group_inputs", "index": GROUP_HERRING_A,
                    "variable": "vul_mult", "indices": [pred],
                    "value": 2.0, "navigate": False,
                })
        
        s3 = run_scenario(client, orig,
            name="Reduce Marine Mammal Predation",
            scenario_type="mammal_predation_reduction",
            changes=s3_changes,
            modifications_fn=mod_mammals,
        )
        if s3:
            scenarios.append(s3)
        restore_all_values(client, orig)
        
        # ── Scenario 4: Reduce Salmon Predation ──
        s4_changes = {
            "vul_mult_to_salmon": {
                "predators": PRED_SALMON_HIGH,
                "action": "set to 2.0 (from 100M)",
            }
        }
        
        def mod_salmon(cl):
            for pred in PRED_SALMON_HIGH:
                cl.command_result("set_indexed_value", {
                    "domain": "ecosim_group_inputs", "index": GROUP_HERRING_A,
                    "variable": "vul_mult", "indices": [pred],
                    "value": 2.0, "navigate": False,
                })
        
        s4 = run_scenario(client, orig,
            name="Reduce Salmon Predation",
            scenario_type="salmon_predation_reduction",
            changes=s4_changes,
            modifications_fn=mod_salmon,
        )
        if s4:
            scenarios.append(s4)
        restore_all_values(client, orig)
        
        # ── Scenario 5: Increase Zooplankton Production ──
        s5_changes = {
            "zooplankton_max_rel_pb": {"from": orig["zooplankton_max_rel_pb"], "to": 5.0}
        }
        
        def mod_zoo(cl):
            cl.command_result("set_values", {
                "domain": "ecosim_group_inputs", "index": GROUP_ZOOPLANKTON,
                "values": {"max_rel_pb": 5.0}, "navigate": False,
            })
        
        s5 = run_scenario(client, orig,
            name="Increase Zooplankton Production",
            scenario_type="bottom_up_enhancement",
            changes=s5_changes,
            modifications_fn=mod_zoo,
        )
        if s5:
            scenarios.append(s5)
        restore_all_values(client, orig)
        
        # ── Scenario 6: Groundfish Predation Reduction ──
        s6_changes = {
            "vul_mult_to_groundfish": {
                "predators": PRED_GROUNDFISH,
                "action": "set to 1.0 (minimum, from 2.0)",
            }
        }
        
        def mod_gf(cl):
            for pred in PRED_GROUNDFISH:
                cl.command_result("set_indexed_value", {
                    "domain": "ecosim_group_inputs", "index": GROUP_HERRING_A,
                    "variable": "vul_mult", "indices": [pred],
                    "value": 1.0, "navigate": False,
                })
        
        s6 = run_scenario(client, orig,
            name="Groundfish Predation Reduction",
            scenario_type="groundfish_predation_reduction",
            changes=s6_changes,
            modifications_fn=mod_gf,
        )
        if s6:
            scenarios.append(s6)
        restore_all_values(client, orig)
        
        # ── Scenario 7: Combined ──
        s7_changes = {
            "fleet9_pcap_base": {"from": orig["fleet9_pcap_base"], "to": orig["fleet9_pcap_base"] * 0.5},
            "vul_mult_to_mammals": {"predators": PRED_MAMMALS, "action": "set to 2.0"},
            "zooplankton_max_rel_pb": {"from": orig["zooplankton_max_rel_pb"], "to": 5.0},
        }
        
        def mod_combined(cl):
            # Fishing
            cl.command_result("set_values", {
                "domain": "ecosim_fleet_inputs", "index": FLEET_HERRING,
                "values": {"pcap_base": orig["fleet9_pcap_base"] * 0.5},
                "navigate": False,
            })
            # Mammals
            for pred in PRED_MAMMALS:
                cl.command_result("set_indexed_value", {
                    "domain": "ecosim_group_inputs", "index": GROUP_HERRING_A,
                    "variable": "vul_mult", "indices": [pred],
                    "value": 2.0, "navigate": False,
                })
            # Zooplankton
            cl.command_result("set_values", {
                "domain": "ecosim_group_inputs", "index": GROUP_ZOOPLANKTON,
                "values": {"max_rel_pb": 5.0}, "navigate": False,
            })
        
        s7 = run_scenario(client, orig,
            name="Combined (Fishing + Mammals + Zooplankton)",
            scenario_type="combined",
            changes=s7_changes,
            modifications_fn=mod_combined,
        )
        if s7:
            scenarios.append(s7)
        restore_all_values(client, orig)
        
        # ── Step 4: Save results ────────────────────────────────
        result_data = {"scenarios": scenarios}
        
        with open(OUTPUT_PATH, "w") as f:
            json.dump(result_data, f, indent=2)
        print(f"\n{'='*60}")
        print(f"Results saved to: {OUTPUT_PATH}")
        
        # Summary table
        print(f"\n{'='*60}")
        print(f"{'Scenario':<48} {'Herr J end':>10} {'Herr A end':>10} {'J chg':>7} {'A chg':>7}")
        print(f"{'-'*48} {'-'*10} {'-'*10} {'-'*7} {'-'*7}")
        for s in scenarios:
            hj = s["herring_j"]
            ha = s["herring_a"]
            print(f"{s['name']:<48} {hj['end']:>8.4f}  {ha['end']:>8.4f}  {hj['change_pct']:>+5.1f}% {ha['change_pct']:>+5.1f}%")
        
        # Rank by herring A end biomass
        ranked = sorted(scenarios, key=lambda s: s["herring_a"]["end"], reverse=True)
        print(f"\n{'='*60}")
        print("Top 3 scenarios by Herring A end biomass (most improvement):")
        for i, s in enumerate(ranked[:3], 1):
            print(f"  {i}. {s['name']}: End={s['herring_a']['end']:.4f} ({s['herring_a']['change_pct']:+.1f}%)")
        
        # Rank by herring J end biomass
        ranked_j = sorted(scenarios, key=lambda s: s["herring_j"]["end"], reverse=True)
        print(f"\nTop 3 scenarios by Herring J end biomass (most improvement):")
        for i, s in enumerate(ranked_j[:3], 1):
            print(f"  {i}. {s['name']}: End={s['herring_j']['end']:.4f} ({s['herring_j']['change_pct']:+.1f}%)")
        
        print(f"\n{'='*60}")
        print("Run complete!")
        
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        client.command_result("close_model", {})
        client.disconnect()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
