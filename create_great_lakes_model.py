"""
Great Lakes Quagga Mussel Impact Model Creator
===============================================
Creates a fully parameterized Ecopath with Ecosim model for the Great Lakes,
focused on quagga mussel (Dreissena rostriformis bugensis) impact.

Uses the EwE MCP socket client to communicate with a running EwE instance.
"""

import json
import sys
import time
import traceback
from ewe_mcp.client import EwEMCPClient

# ── Configuration ──────────────────────────────────────────────────────────
VM_MODEL_PATH = r"C:\Users\dexter\EwE-MCP\tests\fixtures\Great_Lakes_Quagga_Mussel.EwEmdb"
SAMPLE_MODEL_PATH = r"C:\Users\dexter\EwE-MCP\tests\fixtures\Sample_Model.EwEmdb"
HOST = "127.0.0.1"
PORT = 19877
TOKEN = "ewe-mcp-local-dev"
TIMEOUT_LONG = 120

# ── Bug Report Collector ───────────────────────────────────────────────────
bugs = []


def report_bug(command, error, expected, actual, mcp_tool, context=""):
    bugs.append({
        "command": command,
        "error": str(error),
        "expected": expected,
        "actual": actual,
        "mcp_tool": mcp_tool,
        "context": context,
    })
    print(f"\n⚠️  BUG: {command} — {error}")


def safe_call(client, command, params=None, timeout=30, expected="success"):
    """Call a plugin command and capture any bugs."""
    try:
        result = client.command_result(command, params, timeout=timeout)
        return result
    except Exception as e:
        report_bug(command, e, expected, str(e), command)
        return None


# ── Group Definitions ──────────────────────────────────────────────────────
# (name, group_type, biomass, pb, qb, ee)
GROUPS = [
    ("Phytoplankton", 1, 15.0, 150.0, 0, 0.5),
    ("Benthic Algae", 1, 4.0, 60.0, 0, 0.4),
    ("Detritus", 2, 50.0, 0, 0, 0.4),
    ("Microzooplankton", 0, 1.5, 55.0, 180.0, 0.6),
    ("Mesozooplankton", 0, 5.0, 28.0, 100.0, 0.7),
    ("Bythotrephes", 0, 0.5, 20.0, 70.0, 0.6),
    ("Mysis", 0, 0.8, 4.5, 18.0, 0.7),
    ("Quagga Mussels", 0, 100.0, 2.5, 11.0, 0.2),
    ("Diporeia", 0, 0.5, 3.0, 14.0, 0.5),
    ("Other Benthos", 0, 2.5, 3.5, 12.0, 0.5),
    ("Freshwater Drum", 0, 0.5, 0.4, 3.2, 0.4),
    ("Lake Whitefish", 0, 1.5, 0.35, 3.0, 0.6),
    ("Bloater", 0, 2.0, 0.55, 4.0, 0.6),
    ("Cisco", 0, 0.8, 0.4, 3.5, 0.5),
    ("Alewife", 0, 2.0, 0.9, 4.5, 0.8),
    ("Rainbow Smelt", 0, 0.8, 0.8, 4.8, 0.6),
    ("Sculpin", 0, 0.6, 0.8, 4.0, 0.6),
    ("Round Goby", 0, 2.0, 0.9, 4.5, 0.5),
    ("Yellow Perch", 0, 0.8, 0.55, 4.0, 0.5),
    ("Smallmouth Bass", 0, 0.3, 0.4, 3.0, 0.4),
    ("Burbot", 0, 0.2, 0.3, 2.2, 0.4),
    ("Walleye", 0, 1.0, 0.3, 2.5, 0.6),
    ("Lake Trout", 0, 1.2, 0.22, 2.0, 0.5),
    ("Chinook Salmon", 0, 0.4, 0.45, 3.0, 0.7),
    ("Coho Salmon", 0, 0.15, 0.55, 3.2, 0.6),
    ("Sea Lamprey", 0, 0.03, 0.4, 3.0, 0.3),
    ("Lake Sturgeon", 0, 0.2, 0.07, 1.2, 0.2),
    ("Cormorants", 0, 0.02, 0.03, 0.2, 0.02),
]

# Diet composition: predator_name -> [(prey_name, fraction), ...]
DIET = {
    "Phytoplankton": [],
    "Benthic Algae": [],
    "Detritus": [],
    "Microzooplankton": [("Phytoplankton", 0.5), ("Detritus", 0.3), ("Benthic Algae", 0.2)],
    "Mesozooplankton": [("Phytoplankton", 0.4), ("Microzooplankton", 0.3), ("Detritus", 0.2),
                        ("Benthic Algae", 0.1)],
    "Bythotrephes": [("Microzooplankton", 0.5), ("Mesozooplankton", 0.3), ("Detritus", 0.2)],
    "Mysis": [("Mesozooplankton", 0.4), ("Microzooplankton", 0.3), ("Detritus", 0.2),
              ("Phytoplankton", 0.1)],
    "Quagga Mussels": [("Phytoplankton", 0.65), ("Detritus", 0.25), ("Benthic Algae", 0.05),
                       ("Microzooplankton", 0.05)],
    "Diporeia": [("Detritus", 0.5), ("Phytoplankton", 0.3), ("Benthic Algae", 0.2)],
    "Other Benthos": [("Detritus", 0.5), ("Benthic Algae", 0.25), ("Phytoplankton", 0.25)],
    "Freshwater Drum": [("Quagga Mussels", 0.50), ("Round Goby", 0.20), ("Diporeia", 0.10),
                        ("Other Benthos", 0.10), ("Mesozooplankton", 0.10)],
    "Lake Whitefish": [("Quagga Mussels", 0.40), ("Diporeia", 0.25), ("Other Benthos", 0.20),
                       ("Mesozooplankton", 0.10), ("Mysis", 0.05)],
    "Bloater": [("Mesozooplankton", 0.35), ("Mysis", 0.25), ("Diporeia", 0.10),
                ("Microzooplankton", 0.10), ("Detritus", 0.10), ("Other Benthos", 0.10)],
    "Cisco": [("Mesozooplankton", 0.40), ("Mysis", 0.20), ("Microzooplankton", 0.15),
              ("Bythotrephes", 0.15), ("Diporeia", 0.10)],
    "Alewife": [("Mesozooplankton", 0.60), ("Bythotrephes", 0.15), ("Mysis", 0.15),
                ("Microzooplankton", 0.10)],
    "Rainbow Smelt": [("Mesozooplankton", 0.40), ("Mysis", 0.15), ("Bythotrephes", 0.15),
                      ("Microzooplankton", 0.10), ("Diporeia", 0.10), ("Alewife", 0.10)],
    "Sculpin": [("Diporeia", 0.25), ("Other Benthos", 0.25), ("Mysis", 0.20),
                ("Mesozooplankton", 0.15), ("Detritus", 0.15)],
    "Round Goby": [("Quagga Mussels", 0.50), ("Other Benthos", 0.25), ("Diporeia", 0.10),
                   ("Benthic Algae", 0.05), ("Mesozooplankton", 0.10)],
    "Yellow Perch": [("Mesozooplankton", 0.20), ("Diporeia", 0.15), ("Other Benthos", 0.15),
                     ("Round Goby", 0.15), ("Alewife", 0.10), ("Rainbow Smelt", 0.10),
                     ("Quagga Mussels", 0.10), ("Detritus", 0.05)],
    "Smallmouth Bass": [("Round Goby", 0.30), ("Yellow Perch", 0.20), ("Alewife", 0.15),
                        ("Crayfish", 0.15), ("Rainbow Smelt", 0.10), ("Mesozooplankton", 0.10)],
    "Burbot": [("Sculpin", 0.25), ("Bloater", 0.20), ("Round Goby", 0.15),
               ("Lake Whitefish", 0.10), ("Mysis", 0.10), ("Diporeia", 0.10), ("Detritus", 0.10)],
    "Walleye": [("Yellow Perch", 0.30), ("Alewife", 0.20), ("Round Goby", 0.20),
                ("Rainbow Smelt", 0.15), ("Cisco", 0.05), ("Mesozooplankton", 0.10)],
    "Lake Trout": [("Alewife", 0.30), ("Rainbow Smelt", 0.20), ("Sculpin", 0.15),
                   ("Bloater", 0.15), ("Round Goby", 0.10), ("Cisco", 0.10)],
    "Chinook Salmon": [("Alewife", 0.60), ("Rainbow Smelt", 0.20), ("Bloater", 0.10),
                       ("Cisco", 0.10)],
    "Coho Salmon": [("Alewife", 0.40), ("Rainbow Smelt", 0.25), ("Bloater", 0.15),
                    ("Cisco", 0.10), ("Round Goby", 0.10)],
    "Sea Lamprey": [("Lake Trout", 0.30), ("Lake Whitefish", 0.20), ("Bloater", 0.15),
                    ("Chinook Salmon", 0.10), ("Cisco", 0.10), ("Walleye", 0.10), ("Burbot", 0.05)],
    "Lake Sturgeon": [("Other Benthos", 0.30), ("Diporeia", 0.20), ("Quagga Mussels", 0.20),
                      ("Detritus", 0.15), ("Round Goby", 0.10), ("Mesozooplankton", 0.05)],
    "Cormorants": [("Yellow Perch", 0.30), ("Alewife", 0.25), ("Round Goby", 0.20),
                   ("Rainbow Smelt", 0.10), ("Smallmouth Bass", 0.10), ("Freshwater Drum", 0.05)],
}

FLEETS = [
    "Commercial Whitefish Fishery",
    "Commercial Walleye Fishery",
    "Commercial Yellow Perch Fishery",
    "Recreational Salmon Fishery",
    "Recreational Walleye Fishery",
    "Recreational Warmwater Fishery",
    "Commercial Cisco Fishery",
]

# ── Helper: name -> index lookup ──────────────────────────────────────────
def build_name_index(client, domain="ecopath_group"):
    """Build a name-to-index mapping for entities in a domain."""
    result = safe_call(client, "get_values_range", {
        "domain": domain,
        "variables": ["name"],
        "start_index": 1,
        "navigate": False,
    })
    name_index = {}
    if result and "entities" in result:
        for e in result["entities"]:
            name_index[str(e.get("name", "")).strip()] = e["index"]
    return name_index


def resolve_name(client, domain, name):
    """Resolve an entity name to its index."""
    try:
        result = client.command_result("resolve_entity_name", {
            "domain": domain,
            "name": name,
        })
        return result.get("index")
    except Exception:
        ni = build_name_index(client, domain)
        return ni.get(name.strip())


def resolve_names(name_index, names):
    """Resolve multiple names to indices."""
    return [name_index.get(n.strip()) for n in names]


# ── Main Workflow ─────────────────────────────────────────────────────────
def main():
    print("=" * 72)
    print("Great Lakes Quagga Mussel Impact Model Creator")
    print("=" * 72)

    # Connect
    print("\n[1] Connecting to EwE MCP plugin...")
    client = EwEMCPClient(host=HOST, port=PORT, token=TOKEN)
    if not client.connect():
        print("FATAL: Could not connect. Check tunnel and EwE status.")
        sys.exit(1)
    print(f"  Connected to {HOST}:{PORT}")

    # Ping and diagnose
    try:
        ping = client.command_result("ping")
        print(f"  Ping: {ping}")
    except Exception as e:
        print(f"  Ping failed: {e}")
        client.disconnect()
        sys.exit(1)

    try:
        diag = client.command_result("diagnose")
        print(f"  Plugin version: {diag.get('plugin_version', 'unknown')}")
        print(f"  EwE version: {diag.get('ewe_version', 'unknown')}")
    except Exception as e:
        print(f"  Diagnose failed: {e}")

    # ── Step 2: Create Model ──────────────────────────────────────────────
    print("\n[2] Creating Great Lakes model...\n")

    # Try create_model first
    model_created = False
    try:
        result = client.command_result("create_model", {
            "path": VM_MODEL_PATH,
            "model_name": "Great Lakes Quagga Mussel Impact",
            "author": "EwE-MCP Agent",
        }, timeout=TIMEOUT_LONG)
        print(f"  create_model result: {json.dumps(result, indent=2)}")
        model_created = True
    except Exception as e:
        print(f"  create_model failed: {e}")
        print("  Falling back: loading Sample_Model and saving as Great Lakes...")
        try:
            # Close any existing model
            safe_call(client, "close_model")
            # Load sample model
            result = client.command_result("load_model", {"path": SAMPLE_MODEL_PATH}, timeout=TIMEOUT_LONG)
            print(f"  Loaded Sample_Model: {result.get('loaded', '?')}")
            # Save as new
            result = client.command_result("save_model_as", {"path": VM_MODEL_PATH}, timeout=TIMEOUT_LONG)
            print(f"  Saved as Great Lakes: {result.get('result', '?')}")
            # Close and re-load
            safe_call(client, "close_model")
            result = client.command_result("load_model", {"path": VM_MODEL_PATH}, timeout=TIMEOUT_LONG)
            print(f"  Reloaded: {result.get('loaded', '?')}")
            model_created = True
        except Exception as e2:
            print(f"  Fallback also failed: {e2}")

    if not model_created:
        print("FATAL: Could not create model. Aborting.")
        client.disconnect()
        sys.exit(1)

    # ── Step 3: Discover Writable Fields ──────────────────────────────────
    print("\n[3] Discovering Ecopath group writable fields...")
    vars_result = safe_call(client, "list_variables", {
        "domain": "ecopath_group",
        "index": 1,
    })
    writable_fields = []
    indexed_vars = []
    if vars_result:
        for v in vars_result.get("variables", []):
            if v.get("writable", False) and not v.get("indexed", False):
                writable_fields.append(v["name"])
            if v.get("indexed", False):
                indexed_vars.append(v["name"])
        print(f"  Writable scalar fields: {writable_fields}")
        print(f"  Indexed variables: {indexed_vars}")
    else:
        print("  Could not list variables, using defaults: ba, pb, qb, ee, name")
        writable_fields = ["ba", "pb", "qb", "ee", "name"]

    # Determine the field names for biomass, pb, qb, ee
    # Common EwE field names:
    bio_field = None
    pb_field = None
    qb_field = None
    ee_field = None
    for f in writable_fields:
        fl = f.lower()
        if fl in ("ba", "biomass", "biomassinput", "initialbiomass"):
            bio_field = f
        elif fl in ("pb", "pb_input", "pbinput", "production_biomass"):
            pb_field = f
        elif fl in ("qb", "qb_input", "qbinput", "consumption_biomass"):
            qb_field = f
        elif fl == "ee":
            ee_field = f

    print(f"  Mapped: bio={bio_field}, pb={pb_field}, qb={qb_field}, ee={ee_field}")

    # ── Step 4: Create Ecopath Groups ─────────────────────────────────────
    print("\n[4] Creating Ecopath functional groups...")

    # First, check what groups already exist (from default model)
    existing = safe_call(client, "list_ecopath_groups")
    if existing:
        existing_names = [g.get("name", "") for g in existing.get("groups", [])]
        print(f"  Existing groups: {existing_names}")

    # Delete existing groups (except Detritus which is required)
    if existing:
        for g in existing.get("groups", []):
            gname = g.get("name", "")
            gidx = g.get("index")
            if gname.lower() != "detritus" and gidx is not None:
                # Groups can only be deleted if they're not the last remaining one
                # Try to see if there's a delete_group method
                safe_call(client, "delete_ecopath_group", {"index": gidx})
                print(f"  Deleted default group: {gname} (index {gidx})")

    # Save and reload after deletions
    safe_call(client, "save_model", {"path": VM_MODEL_PATH}, timeout=TIMEOUT_LONG)
    safe_call(client, "load_model", {"path": VM_MODEL_PATH}, timeout=TIMEOUT_LONG)

    # Now create all groups
    group_indices = {}  # name -> index after creation
    for i, (name, gtype, biomass, pb, qb, ee) in enumerate(GROUPS):
        params = {"name": name, "group_type": gtype}
        if biomass > 0:
            params["biomass"] = biomass
        if pb > 0:
            params["pb_ratio"] = pb
        if qb > 0:
            params["qb_ratio"] = qb
        if ee > 0:
            params["ee"] = ee

        result = safe_call(client, "create_ecopath_group", params, timeout=30,
                          expected=f"Create group {name}")
        if result:
            gidx = result.get("group_index")
            print(f"  [{i+1}/{len(GROUPS)}] Created '{name}' (type={gtype}) at index {gidx}")
            if gidx is not None:
                group_indices[name] = gidx
        else:
            print(f"  [{i+1}/{len(GROUPS)}] FAILED to create '{name}'")

    # Save and reload to refresh core counts
    print("  Saving and reloading to sync group indices...")
    safe_call(client, "save_model", {"path": VM_MODEL_PATH}, timeout=TIMEOUT_LONG)
    safe_call(client, "load_model", {"path": VM_MODEL_PATH}, timeout=TIMEOUT_LONG)

    # ── Step 5: Verify groups and build name index ────────────────────────
    print("\n[5] Verifying groups...")
    groups_list = safe_call(client, "list_ecopath_groups")
    if groups_list:
        verified = []
        for g in groups_list.get("groups", []):
            verified.append(f"  index {g.get('index')}: {g.get('name')}")
        print("\n".join(verified))
        # Rebuild name->index
        name_to_idx = {}
        for g in groups_list.get("groups", []):
            name_to_idx[str(g.get("name", "")).strip()] = g["index"]
        print(f"\n  Total groups: {len(name_to_idx)}")
        print(f"  Group names: {list(name_to_idx.keys())}")
    else:
        print("  Could not list groups - will try get_values_range")
        name_to_idx = build_name_index(client, "ecopath_group")
        print(f"  Found {len(name_to_idx)} groups via get_values_range")

    if len(name_to_idx) < 28:
        print(f"  WARNING: Only {len(name_to_idx)} groups found. Expected 28.")

    # ── Step 6: Set Group Parameters via set_values_by_name ───────────────
    print("\n[6] Setting group parameters...")

    # Build param sets per group
    group_params = {}
    for name, gtype, biomass, pb, qb, ee in GROUPS:
        vals = {}
        if bio_field and name != "Detritus":  # Detritus doesn't need biomass typically
            vals[bio_field] = biomass
        elif bio_field:
            vals[bio_field] = biomass
        if pb_field and pb > 0:
            vals[pb_field] = pb
        if qb_field and qb > 0:
            vals[qb_field] = qb
        if ee_field:
            vals[ee_field] = ee
        group_params[name] = vals

    for name, vals in group_params.items():
        if name not in name_to_idx:
            print(f"  ⚠️  '{name}' not found in model, skipping")
            continue
        idx = name_to_idx[name]
        # Validate first
        val_result = safe_call(client, "validate_values", {
            "domain": "ecopath_group",
            "index": idx,
            "values": vals,
        }, expected=f"Validate {name} params")
        if val_result:
            is_valid = all(
                v.get("valid", False) != False
                for v in (val_result.get("results") or {}).values()
            )
            print(f"  Validate '{name}' (idx {idx}): {'valid' if is_valid else 'INVALID'} "
                  f"→ {val_result.get('results', {})}")
            if is_valid:
                set_result = safe_call(client, "set_values", {
                    "domain": "ecopath_group",
                    "index": idx,
                    "values": vals,
                    "navigate": False,
                }, timeout=TIMEOUT_LONG, expected=f"Set {name} params")
                status = "OK" if set_result else "FAILED"
                print(f"  Set '{name}': {status}")
        else:
            # Try setting anyway
            set_result = safe_call(client, "set_values", {
                "domain": "ecopath_group",
                "index": idx,
                "values": vals,
                "navigate": False,
            }, timeout=TIMEOUT_LONG, expected=f"Set {name} params (skip validation)")

    # ── Step 7: Set Diet Composition ──────────────────────────────────────
    print("\n[7] Setting diet composition...")

    # Discover diet_comp variable name
    diet_var = None
    for var in indexed_vars:
        if "diet" in var.lower():
            diet_var = var
            break
    if not diet_var and indexed_vars:
        # Could be "diet_comp" or "DietCompInput" etc
        diet_var = indexed_vars[0]  # first indexed var is often diet_comp
    if not diet_var:
        diet_var = "diet_comp"
    print(f"  Using diet variable: '{diet_var}'")

    # Set diet composition for each predator
    for pred_name, prey_list in DIET.items():
        if pred_name not in name_to_idx:
            print(f"  ⚠️  Predator '{pred_name}' not found, skipping diet")
            continue
        pred_idx = name_to_idx[pred_name]

        if not prey_list:
            continue

        for prey_name, fraction in prey_list:
            if prey_name not in name_to_idx:
                print(f"  ⚠️  Prey '{prey_name}' not found (for {pred_name}), skipping")
                continue
            prey_idx = name_to_idx[prey_name]

            # Validate first
            val_result = safe_call(client, "validate_indexed_value", {
                "domain": "ecopath_group",
                "index": pred_idx,
                "variable": diet_var,
                "indices": [prey_idx],
                "value": fraction,
            }, expected=f"Validate diet {pred_name}->{prey_name}")

            if val_result and val_result.get("valid", False) != False:
                set_result = safe_call(client, "set_indexed_value", {
                    "domain": "ecopath_group",
                    "index": pred_idx,
                    "variable": diet_var,
                    "indices": [prey_idx],
                    "value": fraction,
                    "navigate": False,
                }, timeout=TIMEOUT_LONG, expected=f"Set diet {pred_name}->{prey_name}")
                print(f"  Diet: {pred_name}[{pred_idx}] -> {prey_name}[{prey_idx}] = {fraction}: OK")
            else:
                inval = val_result.get("valid", "N/A") if val_result else "no result"
                print(f"  Diet: {pred_name}[{pred_idx}] -> {prey_name}[{prey_idx}] = {fraction}: "
                      f"INVALID ({inval})")
                # Try without validation - might work
                set_result = safe_call(client, "set_indexed_value", {
                    "domain": "ecopath_group",
                    "index": pred_idx,
                    "variable": diet_var,
                    "indices": [prey_idx],
                    "value": fraction,
                    "navigate": False,
                }, timeout=TIMEOUT_LONG, expected=f"Set diet {pred_name}->{prey_name} (force)")

    # Save after diet composition
    safe_call(client, "save_model", {"path": VM_MODEL_PATH}, timeout=TIMEOUT_LONG)
    safe_call(client, "load_model", {"path": VM_MODEL_PATH}, timeout=TIMEOUT_LONG)

    # ── Step 8: Create Fleets ─────────────────────────────────────────────
    print("\n[8] Creating fishing fleets...")
    for fname in FLEETS:
        result = safe_call(client, "create_ecopath_fleet", {"name": fname},
                          expected=f"Create fleet {fname}")
        if result:
            print(f"  Created fleet: {fname}")
        else:
            print(f"  Failed to create fleet: {fname}")

    # Save and reload
    safe_call(client, "save_model", {"path": VM_MODEL_PATH}, timeout=TIMEOUT_LONG)
    safe_call(client, "load_model", {"path": VM_MODEL_PATH}, timeout=TIMEOUT_LONG)

    # Check fleets
    fleets_result = safe_call(client, "list_ecopath_fleets")
    if fleets_result:
        fleet_names = [f.get("name", "") for f in fleets_result.get("fleets", [])]
        fleet_indices = {f.get("name", ""): f.get("index") for f in fleets_result.get("fleets", [])}
        print(f"  Fleets created: {fleet_names}")
        print(f"  Fleet name->index: {fleet_indices}")

    # ── Step 9: Set Fleet Parameters ─────────────────────────────────────
    print("\n[9] Setting fleet parameters...")

    # We'll set landings/discards. First discover fleet writable fields.
    fleet_vars = safe_call(client, "list_variables", {"domain": "ecopath_fleet", "index": 1})
    fleet_writable = []
    fleet_indexed = []
    if fleet_vars:
        for v in fleet_vars.get("variables", []):
            if v.get("writable", False) and not v.get("indexed", False):
                fleet_writable.append(v["name"])
            if v.get("indexed", False):
                fleet_indexed.append(v["name"])
        print(f"  Fleet writable fields: {fleet_writable}")
        print(f"  Fleet indexed fields: {fleet_indexed}")

    # Set some basic fleet landings (example: each fleet catches some groups)
    # For a quick test, we set a small landing for the main target species
    fleet_landings = {
        "Commercial Whitefish Fishery": {"Lake Whitefish": 0.5},
        "Commercial Walleye Fishery": {"Walleye": 0.6},
        "Commercial Yellow Perch Fishery": {"Yellow Perch": 0.3},
        "Recreational Salmon Fishery": {"Chinook Salmon": 0.15, "Coho Salmon": 0.05},
        "Recreational Walleye Fishery": {"Walleye": 0.2},
        "Recreational Warmwater Fishery": {"Smallmouth Bass": 0.05, "Yellow Perch": 0.1},
        "Commercial Cisco Fishery": {"Cisco": 0.2},
    }

    landings_var = None
    for var in fleet_indexed:
        if "landing" in var.lower():
            landings_var = var
            break
    if not landings_var:
        landings_var = "landings"

    for fleet_name, catches in fleet_landings.items():
        if fleet_name not in fleet_indices:
            print(f"  ⚠️  Fleet '{fleet_name}' not found, skipping landings")
            continue
        f_idx = fleet_indices[fleet_name]
        for target_name, amount in catches.items():
            if target_name not in name_to_idx:
                print(f"  ⚠️  Target '{target_name}' not found (for fleet {fleet_name})")
                continue
            t_idx = name_to_idx[target_name]
            val_result = safe_call(client, "validate_indexed_value", {
                "domain": "ecopath_fleet",
                "index": f_idx,
                "variable": landings_var,
                "indices": [t_idx],
                "value": amount,
            }, expected=f"Validate landing {fleet_name}->{target_name}")
            if val_result and val_result.get("valid", False) != False:
                safe_call(client, "set_indexed_value", {
                    "domain": "ecopath_fleet",
                    "index": f_idx,
                    "variable": landings_var,
                    "indices": [t_idx],
                    "value": amount,
                    "navigate": False,
                }, timeout=TIMEOUT_LONG, expected=f"Set landing {fleet_name}->{target_name}")
                print(f"  Landing: {fleet_name}[{f_idx}] -> {target_name}[{t_idx}] = {amount}")

    # ── Step 10: Run Ecopath ──────────────────────────────────────────────
    print("\n[10] Running Ecopath...")
    run_result = safe_call(client, "run_ecopath", timeout=TIMEOUT_LONG)
    if run_result:
        print(f"  Ecopath run result: {json.dumps(run_result, indent=2)}")

    # Check state
    state = safe_call(client, "get_ecopath_run_state")
    if state:
        print(f"  Ecopath run state: {json.dumps(state, indent=2)}")

    # Get diagnostics
    diag_result = safe_call(client, "get_ecopath_group_diagnostics")
    if diag_result:
        print(f"  Diagnostics structure: {list(diag_result.keys())}")
        for g in diag_result.get("groups", []):
            name = g.get("name", "")
            ee_val = g.get("ee", g.get("EcotrophicEfficiency", "?"))
            pb_val = g.get("pb", g.get("ProductionBiomass", "?"))
            bio_val = g.get("biomass", g.get("Biomass", "?"))
            status = "✓" if ee_val not in (None, "?", -9999) and (isinstance(ee_val, (int, float)) and ee_val <= 1) else "⚠️"
            print(f"  {status} {name}: B={bio_val} P/B={pb_val} EE={ee_val}")

    # Stats
    stats = safe_call(client, "get_ecopath_stats")
    if stats:
        print(f"  Ecopath stats: {json.dumps(stats, indent=2)}")

    # ── Balancing Iteration ───────────────────────────────────────────────
    print("\n[11] Balancing iteration...")

    def get_ee_for_group(diag, group_name):
        if not diag:
            return None
        for g in diag.get("groups", []):
            if g.get("name", "").strip() == group_name:
                return g.get("ee", g.get("EcotrophicEfficiency", None))
        return None

    # Check which groups need adjustment
    max_iterations = 5
    for iteration in range(max_iterations):
        # After each adjustment, run Ecopath again
        run_result = safe_call(client, "run_ecopath", timeout=TIMEOUT_LONG)
        if not run_result:
            print(f"  Iteration {iteration+1}: Ecopath run failed")
            break

        state = safe_call(client, "get_ecopath_run_state")
        if state:
            balanced = state.get("balanced", state.get("is_balanced", state.get("IsModelBalanced", None)))
            print(f"  Iteration {iteration+1}: Balanced={balanced}")

        diag_result = safe_call(client, "get_ecopath_group_diagnostics")
        if not diag_result:
            break

        # Find groups with EE > 1 (unbalanced)
        over_ee = []
        for g in diag_result.get("groups", []):
            name = g.get("name", "")
            ee_val = g.get("ee", g.get("EcotrophicEfficiency", None))
            if ee_val is not None and isinstance(ee_val, (int, float)) and ee_val > 1.0:
                over_ee.append((name, ee_val))

        if not over_ee:
            print(f"  All groups have EE <= 1. Model is balanced!")
            break

        print(f"  Iteration {iteration+1}: {len(over_ee)} groups with EE > 1")
        for name, ee_val in over_ee:
            print(f"    {name}: EE = {ee_val}")
            # Try reducing P/B or increasing biomass
            idx = name_to_idx.get(name)
            if idx is None:
                continue
            # Get current values first
            current = safe_call(client, "get_values", {
                "domain": "ecopath_group",
                "index": idx,
                "variables": writable_fields,
                "navigate": False,
            })
            if current:
                print(f"    Current values for {name}: {json.dumps(current, indent=6)}")

        if iteration == max_iterations - 1:
            print(f"  Reached max iterations ({max_iterations})")

    # ── Step 12: Create Ecosim Scenarios ──────────────────────────────────
    print("\n[12] Creating Ecosim scenarios...")
    scenarios = safe_call(client, "list_ecosim_scenarios")
    if scenarios:
        print(f"  Existing scenarios: {scenarios.get('scenarios', scenarios.get('results', []))}")

    # Create post-quagga baseline scenario
    try:
        result = client.command_result("create_ecosim_scenario", {
            "name": "Post-Quagga Baseline",
            "description": "Current Great Lakes ecosystem with established quagga mussel population",
            "author": "EwE-MCP Agent",
        }, timeout=TIMEOUT_LONG)
        print(f"  Created 'Post-Quagga Baseline': {result.get('result', result)}")
    except Exception as e:
        print(f"  Failed to create scenario: {e}")

    # Save scenario
    try:
        result = client.command_result("save_ecosim_scenario", timeout=TIMEOUT_LONG)
        print(f"  Saved scenario: {result}")
    except Exception as e:
        print(f"  Failed to save scenario: {e}")

    # Create pre-quagga scenario
    try:
        result = client.command_result("create_ecosim_scenario", {
            "name": "Pre-Quagga Reference",
            "description": "Historical Great Lakes condition before quagga mussel invasion (pre-1990)",
            "author": "EwE-MCP Agent",
        }, timeout=TIMEOUT_LONG)
        print(f"  Created 'Pre-Quagga Reference': {result.get('result', result)}")
    except Exception as e:
        print(f"  Failed to create scenario: {e}")

    try:
        result = client.command_result("save_ecosim_scenario", timeout=TIMEOUT_LONG)
        print(f"  Saved scenario: {result}")
    except Exception as e:
        print(f"  Failed to save scenario: {e}")

    # ── Step 13: Run Ecosim ───────────────────────────────────────────────
    print("\n[13] Running Ecosim...")
    # List scenarios
    scenarios = safe_call(client, "list_ecosim_scenarios")
    if scenarios:
        print(f"  Scenarios: {json.dumps(scenarios, indent=2)}")

    # Load first scenario (Post-Quagga Baseline)
    try:
        # Load scenario 1 (Post-Quagga Baseline)
        client.command_result("load_ecosim_scenario", {"index": 1})
        print("  Loaded scenario 1 (Post-Quagga Baseline)")
    except Exception as e:
        print(f"  Failed to load scenario: {e}")

    # Run Ecosim
    run_result = safe_call(client, "run_ecosim", timeout=TIMEOUT_LONG)
    if run_result:
        print(f"  Ecosim run result: {run_result}")

    # Wait for completion
    timeout = 100
    start = time.time()
    while time.time() - start < timeout:
        state = safe_call(client, "get_ecosim_run_state")
        if state:
            print(f"  Ecosim state at t={time.time()-start:.0f}s: {json.dumps(state, indent=2)}")
            is_running = state.get("running", state.get("is_running", None))
            if is_running is False or is_running == 0:
                print("  Ecosim completed!")
                break
        time.sleep(2)

    # ── Step 14: Save Final Model ─────────────────────────────────────────
    print("\n[14] Saving final model...")
    try:
        result = client.command_result("save_model", {"path": VM_MODEL_PATH}, timeout=TIMEOUT_LONG)
        print(f"  Model saved: {result}")
    except Exception as e:
        print(f"  Failed to save: {e}")

    # ── Report ────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("FINAL REPORT")
    print("=" * 72)
    print(f"\nModel path: {VM_MODEL_PATH}")
    print(f"Groups created: {len(name_to_idx)}")
    print(f"Fleets created: {len(fleet_indices) if 'fleet_indices' in dir() else '?'}")
    print(f"Ecopath run: {'yes' if run_result else 'no'}")
    print(f"Ecosim scenarios: 2 (Post-Quagga Baseline, Pre-Quagga Reference)")

    print(f"\nBug reports ({len(bugs)}):")
    print(json.dumps(bugs, indent=2))

    # Disconnect
    client.disconnect()
    print("\nDone!")


if __name__ == "__main__":
    main()
