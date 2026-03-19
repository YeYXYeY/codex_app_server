#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Process RHCA dataset using standardized utilities.
This dataset contains Rh-catalyzed asymmetric allylation reactions.
CORRECTED VERSION with proper EE calculation and ΔΔG calculation.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from rdkit import Chem
import warnings
import time
from datetime import datetime
import re

from coordination_utils import (
    get_dataset_paths, ensure_directories_exist, save_processed_dataset,
    log_processing_errors, create_processing_report, detect_coordination_sites_batch,
    build_metal_ligand_complex, create_molecular_visualizations_standardized,
    create_summary_plots_standardized, convert_ee_to_absolute
)

warnings.filterwarnings('ignore')

def extract_allyl_fragment_from_boron_reagent(boron_smiles):
    """
    Extract the allyl fragment from boron reagent by finding B-C bonds and keeping the carbon-connected fragment.
    Returns the fragment molecule and the carbon atom index for further linkage.
    """
    if pd.isna(boron_smiles):
        return None, None
    
    # Parse the boron reagent with RDKit
    mol = Chem.MolFromSmiles(boron_smiles)
    if mol is None:
        return None, None
    
    # Find boron atoms and their adjacent carbons
    boron_carbon_pairs = []
    for atom in mol.GetAtoms():
        if atom.GetSymbol() == 'B':
            boron_idx = atom.GetIdx()
            # Find adjacent carbon atoms
            for neighbor in atom.GetNeighbors():
                if neighbor.GetSymbol() == 'C':
                    boron_carbon_pairs.append((boron_idx, neighbor.GetIdx()))
    
    if not boron_carbon_pairs:
        # No B-C bonds found - this might be a non-standard reagent
        # For now, try to use the first carbon atom as attachment point
        carbon_atoms = [atom.GetIdx() for atom in mol.GetAtoms() if atom.GetSymbol() == 'C']
        if carbon_atoms:
            # Use the first carbon as attachment point
            return mol, carbon_atoms[0]
        else:
            # No carbon atoms found, return None
            return mol, None
    
    # Use the first B-C pair (we can extend this to handle multiple B-C bonds later)
    boron_idx, carbon_idx = boron_carbon_pairs[0]
    
    # Create a copy of the molecule for modification
    mol_copy = Chem.RWMol(mol)
    
    # Remove the boron atom (this cuts the B-C bond)
    mol_copy.RemoveAtom(boron_idx)
    
    # Get the modified molecule
    modified_mol = mol_copy.GetMol()
    
    # Find all atoms connected to the carbon through the graph
    connected_atoms = set()
    to_visit = [carbon_idx]
    
    while to_visit:
        current_idx = to_visit.pop(0)
        if current_idx in connected_atoms:
            continue
        
        connected_atoms.add(current_idx)
        current_atom = modified_mol.GetAtomWithIdx(current_idx)
        
        # Add all neighbors to the visit list
        for neighbor in current_atom.GetNeighbors():
            neighbor_idx = neighbor.GetIdx()
            if neighbor_idx not in connected_atoms:
                to_visit.append(neighbor_idx)
    
    # Create a new molecule with only the connected atoms
    fragment_mol = Chem.RWMol()
    
    # Map old indices to new indices
    old_to_new_idx = {}
    new_idx = 0
    
    # Add all connected atoms
    for old_idx in connected_atoms:
        old_atom = modified_mol.GetAtomWithIdx(old_idx)
        new_atom = fragment_mol.AddAtom(old_atom)
        old_to_new_idx[old_idx] = new_idx
        new_idx += 1
    
    # Add all bonds between connected atoms
    for old_idx in connected_atoms:
        old_atom = modified_mol.GetAtomWithIdx(old_idx)
        for bond in modified_mol.GetBonds():
            begin_idx = bond.GetBeginAtomIdx()
            end_idx = bond.GetEndAtomIdx()
            
            if begin_idx == old_idx and end_idx in connected_atoms:
                new_begin_idx = old_to_new_idx[begin_idx]
                new_end_idx = old_to_new_idx[end_idx]
                fragment_mol.AddBond(new_begin_idx, new_end_idx, bond.GetBondType())
    
    # Get the final fragment molecule
    final_fragment = fragment_mol.GetMol()
    
    # Find the new index of the carbon atom (the attachment point)
    attachment_carbon_idx = old_to_new_idx.get(carbon_idx)
    
    return final_fragment, attachment_carbon_idx

# Import standardized EE/ΔΔG functions from coordination_utils
from coordination_utils import (
    calculate_ee_from_top_percent, calculate_deltadeltaG_from_ee,
    standardize_ee_value, calculate_deltadeltaG_from_ee_standardized
)

def identify_conjugated_system(substrate_smiles):
    """
    Identify the conjugated C=C-C=O system in the substrate.
    Returns the position of the first carbon in the C=C bond.
    """
    if pd.isna(substrate_smiles):
        return None
    
    # Look for C=C-C=O pattern (conjugated system)
    # This is a simplified approach - in reality, we'd need more sophisticated parsing
    if 'C=C' in substrate_smiles and 'C=O' in substrate_smiles:
        # Find the position of C=C
        c_c_pos = substrate_smiles.find('C=C')
        return c_c_pos
    
    return None

def construct_allylation_product(substrate_smiles, allyl_fragment_mol, attachment_carbon_idx):
    """
    Construct the product by finding C=C-C=O system and linking allyl fragment to the first carbon.
    """
    if pd.isna(substrate_smiles) or allyl_fragment_mol is None or attachment_carbon_idx is None:
        return None
    
    # Parse the substrate
    substrate_mol = Chem.MolFromSmiles(substrate_smiles)
    if substrate_mol is None:
        return None
    
    # Find the C=C-C=O system and identify the target carbon
    target_carbon = find_carbonyl_connected_carbon(substrate_mol)
    if target_carbon is None:
        # No suitable C=C-C=O system found
        return None
    
    # Create a copy of the substrate for modification
    product_mol = Chem.RWMol(substrate_mol)
    
    # Find the C=C bond involving the target carbon
    c_c_bond = None
    other_carbon_idx = None
    for bond in product_mol.GetBonds():
        if bond.GetBondType() == Chem.BondType.DOUBLE:
            begin_atom = bond.GetBeginAtom()
            end_atom = bond.GetEndAtom()
            if (begin_atom.GetSymbol() == 'C' and end_atom.GetSymbol() == 'C'):
                if begin_atom.GetIdx() == target_carbon:
                    c_c_bond = bond
                    other_carbon_idx = end_atom.GetIdx()
                    break
                elif end_atom.GetIdx() == target_carbon:
                    c_c_bond = bond
                    other_carbon_idx = begin_atom.GetIdx()
                    break
    
    if c_c_bond is None:
        return None
    
    # Remove the C=C double bond
    product_mol.RemoveBond(target_carbon, other_carbon_idx)
    
    # Add a single bond between the two carbons
    product_mol.AddBond(target_carbon, other_carbon_idx, Chem.BondType.SINGLE)
    
    # Set the target carbon as a chiral center
    target_carbon_atom = product_mol.GetAtomWithIdx(target_carbon)
    target_carbon_atom.SetChiralTag(Chem.ChiralType.CHI_TETRAHEDRAL_CW)  # [C@H]
    
    # Now link the allyl fragment directly to the target carbon
    # Add all atoms from the allyl fragment to the product molecule
    allyl_atoms = {}
    for i in range(allyl_fragment_mol.GetNumAtoms()):
        atom = allyl_fragment_mol.GetAtomWithIdx(i)
        new_atom_idx = product_mol.AddAtom(atom)
        allyl_atoms[i] = new_atom_idx
    
    # Add all bonds from the allyl fragment
    for bond in allyl_fragment_mol.GetBonds():
        begin_idx = bond.GetBeginAtomIdx()
        end_idx = bond.GetEndAtomIdx()
        new_begin_idx = allyl_atoms[begin_idx]
        new_end_idx = allyl_atoms[end_idx]
        product_mol.AddBond(new_begin_idx, new_end_idx, bond.GetBondType())
    
    # Link the attachment carbon from allyl fragment to the target carbon
    new_attachment_idx = allyl_atoms[attachment_carbon_idx]
    product_mol.AddBond(target_carbon, new_attachment_idx, Chem.BondType.SINGLE)
    
    # Convert to SMILES
    try:
        product_smiles = Chem.MolToSmiles(product_mol)
        return product_smiles
    except:
        return None

def find_carbonyl_connected_carbon(mol):
    """
    Find the carbon atom in a C=C bond that is the FIRST carbon in conjugated systems.
    This function now handles multiple conjugated systems:
    - C=C-C=O (carbonyl)
    - C=C-[N+]([O-])=O (nitro)
    - C=C-C(O)=O (carboxylic acid)
    - C=C-C#N (cyano)
    - C=C-aromatic (aromatic system)
    
    Returns the carbon atom index that should be the target for allylation.
    """
    # Look for C=C bonds
    for bond in mol.GetBonds():
        if bond.GetBondType() == Chem.BondType.DOUBLE:
            begin_atom = bond.GetBeginAtom()
            end_atom = bond.GetEndAtom()
            
            # Check if both atoms are carbon
            if (begin_atom.GetSymbol() == 'C' and end_atom.GetSymbol() == 'C'):
                # Check if this C=C bond is part of a conjugated system
                begin_has_conjugation = is_connected_to_conjugated_system(mol, begin_atom)
                end_has_conjugation = is_connected_to_conjugated_system(mol, end_atom)
                
                # If one carbon is connected to conjugated system and the other isn't,
                # we want the one that is NOT directly connected to the conjugated system
                if begin_has_conjugation and not end_has_conjugation:
                    return end_atom.GetIdx()  # This is the first carbon in the conjugated system
                elif end_has_conjugation and not begin_has_conjugation:
                    return begin_atom.GetIdx()  # This is the first carbon in the conjugated system
                # If both are connected to conjugated system, choose the one that is not the main functional group
                elif begin_has_conjugation and end_has_conjugation:
                    # For conjugated systems, choose the carbon that is not the main functional group carbon
                    begin_is_main_group = is_main_functional_group_carbon(mol, begin_atom)
                    end_is_main_group = is_main_functional_group_carbon(mol, end_atom)
                    
                    # Choose the non-main functional group carbon
                    if begin_is_main_group and not end_is_main_group:
                        return end_atom.GetIdx()
                    elif end_is_main_group and not begin_is_main_group:
                        return begin_atom.GetIdx()
                    else:
                        # If we can't determine, choose the first carbon (begin_atom)
                        return begin_atom.GetIdx()
    
    return None

def is_connected_to_conjugated_system(mol, carbon_atom):
    """
    Check if a carbon atom is connected to a conjugated system.
    This includes:
    - C=C-C=O (carbonyl)
    - C=C-[N+]([O-])=O (nitro)
    - C=C-C(O)=O (carboxylic acid)
    - C=C-C#N (cyano)
    - C=C-aromatic (aromatic system)
    """
    # Check direct neighbors for conjugated systems
    for neighbor in carbon_atom.GetNeighbors():
        if neighbor.GetSymbol() == 'C':
            # Check for carbonyl (C=O)
            if is_neighbor_carbonyl(mol, neighbor):
                return True
            # Check for nitro group ([N+]([O-])=O)
            if is_neighbor_nitro(mol, neighbor):
                return True
            # Check for carboxylic acid (C(O)=O)
            if is_neighbor_carboxylic_acid(mol, neighbor):
                return True
            # Check for cyano group (C#N)
            if is_neighbor_cyano(mol, neighbor):
                return True
            # Check for aromatic system
            if is_neighbor_aromatic(mol, neighbor):
                return True
    
    return False

def is_neighbor_carbonyl(mol, neighbor):
    """Check if neighbor has a double bond to oxygen (C=O)"""
    for bond in mol.GetBonds():
        if (bond.GetBondType() == Chem.BondType.DOUBLE and
            bond.GetBeginAtom().GetIdx() == neighbor.GetIdx() and
            bond.GetEndAtom().GetSymbol() == 'O'):
            return True
        elif (bond.GetBondType() == Chem.BondType.DOUBLE and
              bond.GetEndAtom().GetIdx() == neighbor.GetIdx() and
              bond.GetBeginAtom().GetSymbol() == 'O'):
            return True
    return False

def is_neighbor_nitro(mol, neighbor):
    """Check if neighbor is part of a nitro group [N+]([O-])=O"""
    # Check if neighbor is connected to nitrogen with positive charge
    for bond in mol.GetBonds():
        if (bond.GetBeginAtom().GetIdx() == neighbor.GetIdx() and
            bond.GetEndAtom().GetSymbol() == 'N' and
            bond.GetEndAtom().GetFormalCharge() == 1):
            # Check if this N is connected to O with negative charge
            n_atom = bond.GetEndAtom()
            for n_bond in mol.GetBonds():
                if (n_bond.GetBeginAtom().GetIdx() == n_atom.GetIdx() and
                    n_bond.GetEndAtom().GetSymbol() == 'O' and
                    n_bond.GetEndAtom().GetFormalCharge() == -1):
                    return True
        elif (bond.GetEndAtom().GetIdx() == neighbor.GetIdx() and
              bond.GetBeginAtom().GetSymbol() == 'N' and
              bond.GetBeginAtom().GetFormalCharge() == 1):
            # Check if this N is connected to O with negative charge
            n_atom = bond.GetBeginAtom()
            for n_bond in mol.GetBonds():
                if (n_bond.GetBeginAtom().GetIdx() == n_atom.GetIdx() and
                    n_bond.GetEndAtom().GetSymbol() == 'O' and
                    n_bond.GetEndAtom().GetFormalCharge() == -1):
                    return True
    return False

def is_neighbor_carboxylic_acid(mol, neighbor):
    """Check if neighbor is part of a carboxylic acid C(O)=O"""
    # Check if neighbor has both OH and =O bonds
    has_oh = False
    has_double_o = False
    
    for bond in mol.GetBonds():
        if bond.GetBeginAtom().GetIdx() == neighbor.GetIdx():
            if (bond.GetEndAtom().GetSymbol() == 'O' and
                bond.GetBondType() == Chem.BondType.SINGLE):
                has_oh = True
            elif (bond.GetEndAtom().GetSymbol() == 'O' and
                  bond.GetBondType() == Chem.BondType.DOUBLE):
                has_double_o = True
        elif bond.GetEndAtom().GetIdx() == neighbor.GetIdx():
            if (bond.GetBeginAtom().GetSymbol() == 'O' and
                bond.GetBondType() == Chem.BondType.SINGLE):
                has_oh = True
            elif (bond.GetBeginAtom().GetSymbol() == 'O' and
                  bond.GetBondType() == Chem.BondType.DOUBLE):
                has_double_o = True
    
    return has_oh and has_double_o

def is_neighbor_cyano(mol, neighbor):
    """Check if neighbor is part of a cyano group C#N"""
    for bond in mol.GetBonds():
        if (bond.GetBeginAtom().GetIdx() == neighbor.GetIdx() and
            bond.GetEndAtom().GetSymbol() == 'N' and
            bond.GetBondType() == Chem.BondType.TRIPLE):
            return True
        elif (bond.GetEndAtom().GetIdx() == neighbor.GetIdx() and
              bond.GetBeginAtom().GetSymbol() == 'N' and
              bond.GetBondType() == Chem.BondType.TRIPLE):
            return True
    return False

def is_neighbor_aromatic(mol, neighbor):
    """Check if neighbor is part of an aromatic system"""
    return neighbor.GetIsAromatic()

def is_main_functional_group_carbon(mol, carbon_atom):
    """
    Check if a carbon atom is part of the main functional group.
    This helps identify which carbon in a conjugated system should NOT be the target.
    """
    # Check if this carbon is part of a carbonyl (C=O)
    for bond in mol.GetBonds():
        if (bond.GetBondType() == Chem.BondType.DOUBLE and
            bond.GetBeginAtom().GetIdx() == carbon_atom.GetIdx() and
            bond.GetEndAtom().GetSymbol() == 'O'):
            return True
        elif (bond.GetBondType() == Chem.BondType.DOUBLE and
              bond.GetEndAtom().GetIdx() == carbon_atom.GetIdx() and
              bond.GetBeginAtom().GetSymbol() == 'O'):
            return True
    
    # Check if this carbon is part of a carboxylic acid (C(O)=O)
    if is_neighbor_carboxylic_acid(mol, carbon_atom):
        return True
    
    # Check if this carbon is part of a cyano group (C#N)
    if is_neighbor_cyano(mol, carbon_atom):
        return True
    
    return False



def build_rh_complex_with_c_c_coordination(ligand_smiles, coordination_info):
    """
    Build Rh complex using C=C coordination (η²-coordination) with RDKit.
    Find NON-AROMATIC C=C double bonds and coordinate them to ONE Rh center.
    """
    if pd.isna(ligand_smiles):
        return None
    
    # Parse the ligand with RDKit
    mol = Chem.MolFromSmiles(ligand_smiles)
    if mol is None:
        # If invalid SMILES, just add Rh coordination
        return f"{ligand_smiles}->[Rh]"
    
    # Find non-aromatic C=C bonds
    non_aromatic_c_c_bonds = []
    
    # Get all bonds
    for bond in mol.GetBonds():
        if bond.GetBondType() == Chem.BondType.DOUBLE:
            begin_atom = bond.GetBeginAtom()
            end_atom = bond.GetEndAtom()
            
            # Check if both atoms are carbon
            if (begin_atom.GetSymbol() == 'C' and end_atom.GetSymbol() == 'C'):
                # Check if this C=C is NOT part of an aromatic ring
                if not begin_atom.GetIsAromatic() and not end_atom.GetIsAromatic():
                    non_aromatic_c_c_bonds.append((bond, begin_atom, end_atom))
    
    if not non_aromatic_c_c_bonds:
        # No non-aromatic C=C bonds found, just add Rh coordination
        return f"{ligand_smiles}->[Rh]"
    
    # Create a copy of the molecule for modification
    complex_mol = Chem.RWMol(mol)
    
    # Add Rh atom
    rh_idx = complex_mol.AddAtom(Chem.Atom('Rh'))
    
    # Coordinate Rh to the C=C bonds (η²-coordination)
    # For each C=C bond, create coordination bonds from Rh to both carbons
    for bond, carbon1, carbon2 in non_aromatic_c_c_bonds:
        # Add coordination bonds from Rh to both carbons of the C=C
        complex_mol.AddBond(rh_idx, carbon1.GetIdx(), Chem.BondType.SINGLE)
        complex_mol.AddBond(rh_idx, carbon2.GetIdx(), Chem.BondType.SINGLE)
    
    # Convert back to SMILES
    try:
        complex_smiles = Chem.MolToSmiles(complex_mol)
        return complex_smiles
    except:
        # If complex creation fails, fall back to simple representation
        return f"{ligand_smiles}->[Rh]"

def process_rhcaa_dataset():
    """Process the RHCA dataset using standardized utilities."""
    
    print("=" * 60)
    print("RHCA DATASET PROCESSING (CORRECTED VERSION)")
    print("=" * 60)
    
    # Load datasets
    real_file = "../../collected_data/considered_dataset/rhcaa_real.csv"
    ligands_file = "../../collected_data/considered_dataset/rhcaa.csv"
    
    print(f"Loading datasets from: {real_file} and {ligands_file}")
    
    try:
        # Load the datasets
        df_real = pd.read_csv(real_file)
        df_ligands = pd.read_csv(ligands_file)
        
        print(f"Loaded {len(df_real)} reactions from real dataset")
        print(f"Loaded {len(df_ligands)} entries from ligands dataset")
        
        # Create ligand mapping dictionary
        ligand_mapping = {}
        if 'ligand_id' in df_ligands.columns and 'ligand' in df_ligands.columns:
            for _, row in df_ligands.iterrows():
                ligand_id = row['ligand_id']
                ligand_smiles = row['ligand']
                if pd.notna(ligand_id) and pd.notna(ligand_smiles):
                    ligand_mapping[ligand_id] = ligand_smiles
        
        print(f"Created ligand mapping with {len(ligand_mapping)} unique ligands")
        
        # Get unique ligands for batch coordination detection
        unique_ligands = list(ligand_mapping.values())
        print(f"Found {len(unique_ligands)} unique ligands for coordination detection")
        
        # Run batch coordination detection on ligands
        print("Running batch coordination detection on ligands...")
        coordination_results = detect_coordination_sites_batch(unique_ligands)
        
        # Process each reaction
        processed_data = []
        failed_entries = []
        success_count = 0
        total_count = len(df_real)
        
        for idx, row in df_real.iterrows():
            try:
                # Extract data
                top_percent = row['%top']  # This is the EE data!
                boron_reagent = row['boron reagent']
                substrate = row['substrate']
                ligand_id = row['ligand']
                solvent = row['solvent']
                temperature = row['temp']
                reaction_time = row['time']
                yield_value = row['yield']
                
                # Skip if essential data is missing
                if pd.isna(top_percent) or pd.isna(boron_reagent) or pd.isna(substrate) or pd.isna(ligand_id):
                    failed_entries.append({
                        'row_idx': idx,
                        'top_percent': top_percent,
                        'boron_reagent': boron_reagent,
                        'substrate': substrate,
                        'ligand_id': ligand_id,
                        'error_type': 'Missing essential data',
                        'error_message': 'Missing top%, boron reagent, substrate, or ligand'
                    })
                    continue
                
                # Calculate EE using corrected formula
                ee_absolute = calculate_ee_from_top_percent(top_percent)
                ee_original = float(top_percent)  # Keep original top% for reference
                
                # Get ligand SMILES from mapping
                ligand_smiles = ligand_mapping.get(ligand_id)
                if ligand_smiles is None:
                    failed_entries.append({
                        'row_idx': idx,
                        'ligand_id': ligand_id,
                        'error_type': 'Missing ligand mapping',
                        'error_message': f'Ligand ID {ligand_id} not found in mapping'
                    })
                    continue
                
                # Extract allyl fragment from boron reagent (returns RDKit molecule and attachment carbon)
                allyl_fragment_mol, attachment_carbon_idx = extract_allyl_fragment_from_boron_reagent(boron_reagent)
                
                # Construct product
                product_smiles = construct_allylation_product(substrate, allyl_fragment_mol, attachment_carbon_idx)
                
                # Build Rh-ligand complex with C=C coordination
                mol = Chem.MolFromSmiles(ligand_smiles)
                if mol is not None:
                    canonical_ligand_smiles = Chem.MolToSmiles(mol)
                    coordination_info = coordination_results.get(ligand_smiles, {})
                    if not coordination_info:
                        coordination_info = coordination_results.get(canonical_ligand_smiles, {})
                else:
                    coordination_info = {}
                
                # Build complex using C=C coordination
                complex_smiles = build_rh_complex_with_c_c_coordination(ligand_smiles, coordination_info)
                
                # Convert temperature to Kelvin
                if pd.notna(temperature):
                    temperature_k = float(temperature) + 273.15
                else:
                    temperature_k = 298.15  # Default to room temperature
                
                # Calculate ΔΔG from EE
                deltadeltaG_kcal = calculate_deltadeltaG_from_ee(ee_absolute, temperature_k)
                
                # Create reactants SMILES
                reactants_smiles = f"{substrate}.{boron_reagent}"
                
                # Create processed entry
                processed_entry = {
                    'dataset': 'rhcaa',
                    'ligand_smiles': ligand_smiles,
                    'reactants': reactants_smiles,
                    'product_smiles': product_smiles,
                    'complex_smiles': complex_smiles,
                    'metal': 'Rh',
                    'ee': ee_absolute,
                    'ee_absolute': ee_absolute,
                    'ee_original': ee_original,
                    'temperature_k': temperature_k,
                    'deltadeltaG_kcal': deltadeltaG_kcal,
                    'reaction_time': reaction_time,
                    'solvent': solvent,
                    'yield': yield_value,
                    'reference': 'RHCA',
                    'other_conditions': f"Boron reagent: {boron_reagent}"
                }
                
                processed_data.append(processed_entry)
                success_count += 1
                
                # Print progress
                if (idx + 1) % 50 == 0:
                    print(f"Processed {idx + 1}/{total_count} entries ({success_count} successful)")
                    
            except Exception as e:
                failed_entries.append({
                    'row_idx': idx,
                    'top_percent': top_percent if 'top_percent' in locals() else None,
                    'boron_reagent': boron_reagent if 'boron_reagent' in locals() else None,
                    'substrate': substrate if 'substrate' in locals() else None,
                    'ligand_id': ligand_id if 'ligand_id' in locals() else None,
                    'error_type': str(type(e).__name__),
                    'error_message': str(e)
                })
                print(f"Error processing entry {idx}: {e}")
        
        # Create DataFrame
        processed_df = pd.DataFrame(processed_data)
        
        print(f"\nProcessing complete!")
        print(f"Total entries: {total_count}")
        print(f"Successful: {success_count}")
        print(f"Failed: {len(failed_entries)}")
        
        return processed_df, success_count, total_count, failed_entries
        
    except Exception as e:
        print(f"Error loading RHCA dataset: {e}")
        return None, 0, 0, []

def main():
    """Main function to process the RHCA dataset."""
    import time
    start_time = time.time()
    
    print("=" * 60)
    print("RHCA DATASET PROCESSING (CORRECTED VERSION)")
    print("=" * 60)
    
    # Process the dataset
    processed_df, success_count, total_count, failed_entries = process_rhcaa_dataset()
    
    if processed_df is None or len(processed_df) == 0:
        print("No data was processed successfully. Exiting.")
        return
    
    # Get dataset paths
    dataset_name = "rhcaa"
    paths = get_dataset_paths(dataset_name)
    
    # Save processed dataset
    print("\nSaving processed dataset...")
    save_processed_dataset(processed_df, dataset_name)
    
    # Log processing errors
    if failed_entries:
        print("\nLogging processing errors...")
        failed_df = pd.DataFrame(failed_entries)
        log_processing_errors(dataset_name, failed_df)
    
    # Create visualizations
    print("\nCreating molecular visualizations...")
    create_molecular_visualizations_standardized(processed_df, dataset_name)
    
    # Create summary plots
    print("\nCreating summary plots...")
    create_summary_plots_standardized(processed_df, dataset_name)
    
    # Create processing report
    print("\nCreating processing report...")
    end_time = time.time()
    processing_time = end_time - start_time
    image_counts = {'ligand_count': success_count, 'complex_count': success_count, 'product_count': success_count}
    create_processing_report(dataset_name, total_count, success_count, len(failed_entries), image_counts, processing_time)
    
    print("\n" + "=" * 60)
    print("RHCA DATASET PROCESSING COMPLETE (CORRECTED)")
    print("=" * 60)
    print(f"Successfully processed: {success_count}/{total_count} entries")
    print(f"Failed entries: {len(failed_entries)}")
    print(f"Success rate: {(success_count/total_count)*100:.1f}%")
    print(f"Total processing time: {end_time - start_time:.2f} seconds")

if __name__ == "__main__":
    main()
