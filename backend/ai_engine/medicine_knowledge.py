"""
ai_engine/medicine_knowledge.py
MediVault — Verified Clinical Pharmacology Knowledge Base

Provides structured, verified clinical medicine information for hospital pharmacy operations:
- Generic and known brand names
- Therapeutic indications & clinical uses
- Contraindications & high-risk warnings
- Important precautions & drug interactions
- Common adverse effects / side effects
- Storage conditions & temperature requirements
- Regulatory schedules (Schedule H, H1, X, Narcotic, General/OTC)

Strict safety compliance:
- Escalate clinical decisions to qualified healthcare professionals.
- No individualized dosage prescription or diagnosis.
"""

# Standard clinical reference database for common active medicines stocked in MediVault
CLINICAL_KNOWLEDGE_BASE = {
    'paracetamol': {
        'display_name': 'Paracetamol (Acetaminophen)',
        'generic_name': 'Paracetamol / Acetaminophen',
        'brand_names': ['Dolo 650', 'Calpol', 'Crocin', 'Pacimol', 'Tylenol'],
        'category': 'Analgesic & Antipyretic',
        'therapeutic_class': 'Non-opioid analgesic / antipyretic',
        'common_uses': (
            'Relief of mild to moderate pain (headache, musculoskeletal pain, toothache, dysmenorrhea) '
            'and reduction of fever associated with infections.'
        ),
        'precautions': (
            'Use with caution in patients with hepatic impairment, chronic malnutrition, dehydration, '
            'or severe renal dysfunction. Maximum daily dose for adults must not exceed 4,000 mg (4 g) '
            'from all sources combined to prevent hepatotoxicity.'
        ),
        'contraindications': (
            'Known severe hypersensitivity to paracetamol. Severe active liver disease or acute hepatic failure.'
        ),
        'side_effects': (
            'Generally well-tolerated at therapeutic doses. Rare adverse reactions include cutaneous hypersensitivity '
            'reactions (rash, erythema, Stevens-Johnson syndrome), thrombocytopenia, and hepatotoxicity in overdose.'
        ),
        'storage_requirements': (
            'Store below 25°C in a dry place. Protect from direct heat, sunlight, and moisture.'
        ),
        'regulatory_schedule': 'General / OTC (Schedule H for specific high-strength intravenous formulations)',
        'pregnancy_lactation': 'Considered safe for short-term use during pregnancy and lactation at standard therapeutic doses under medical advice.',
    },
    'amoxicillin': {
        'display_name': 'Amoxicillin',
        'generic_name': 'Amoxicillin Trihydrate',
        'brand_names': ['Augmentin (with Clavulanate)', 'Mox', 'Novamox', 'Amoxil'],
        'category': 'Antibacterial (Beta-Lactam Antibiotic)',
        'therapeutic_class': 'Broad-spectrum aminopenicillin',
        'common_uses': (
            'Treatment of susceptible bacterial infections including acute otitis media, acute sinusitis, '
            'streptococcal pharyngitis, lower respiratory tract infections, and uncomplicated urinary tract infections.'
        ),
        'precautions': (
            'Screen for previous hypersensitivity to penicillins or beta-lactam antibiotics. Discontinue immediately '
            'if rash or anaphylactic reaction develops. Adjust dose in moderate-to-severe renal impairment (GFR < 30 mL/min).'
        ),
        'contraindications': (
            'Documented anaphylaxis or severe hypersensitivity to penicillins or cephalosporins. History of amoxicillin-associated jaundice.'
        ),
        'side_effects': (
            'Diarrhea, nausea, vomiting, maculopapular rash, candidiasis (oral or vaginal), and rare pseudomembranous colitis (C. difficile).'
        ),
        'storage_requirements': (
            'Store capsules and tablets below 25°C in a dry place. Reconstituted oral suspension must be stored between 2°C and 8°C and discarded after 7–14 days.'
        ),
        'regulatory_schedule': 'Schedule H (Prescription Only)',
        'pregnancy_lactation': 'Pregnancy Category B; compatible with breastfeeding under clinical discretion.',
    },
    'azithromycin': {
        'display_name': 'Azithromycin',
        'generic_name': 'Azithromycin Dihydrate',
        'brand_names': ['Azithral', 'Zithromax', 'Azee', 'Zady'],
        'category': 'Macrolide Antibiotic',
        'therapeutic_class': 'Azalide (Macrolide subclass)',
        'common_uses': (
            'Community-acquired pneumonia, acute bacterial exacerbations of COPD, chlamydial genital infections, '
            'tonsillitis/pharyngitis, and skin/soft tissue infections.'
        ),
        'precautions': (
            'Risk of QT prolongation and ventricular arrhythmias, particularly in elderly patients or those with existing cardiac arrhythmias. '
            'Monitor liver enzymes in patients with pre-existing hepatic disease.'
        ),
        'contraindications': (
            'Known hypersensitivity to azithromycin, erythromycin, or any macrolide. History of cholestatic jaundice with prior azithromycin use.'
        ),
        'side_effects': (
            'Gastrointestinal distress (nausea, abdominal cramping, diarrhea, vomiting), headache, dizziness, transient hearing loss (high doses).'
        ),
        'storage_requirements': (
            'Store below 30°C in a tightly closed container. Protect from moisture and excessive heat.'
        ),
        'regulatory_schedule': 'Schedule H (Prescription Only)',
        'pregnancy_lactation': 'Pregnancy Category B; use only when clinically indicated.',
    },
    'ciprofloxacin': {
        'display_name': 'Ciprofloxacin',
        'generic_name': 'Ciprofloxacin Hydrochloride',
        'brand_names': ['Cifran', 'Ciplox', 'Cipro', 'Ciprobid'],
        'category': 'Fluoroquinolone Antibiotic',
        'therapeutic_class': 'Broad-spectrum fluoroquinolone',
        'common_uses': (
            'Complicated urinary tract infections, pyelonephritis, infectious diarrhea, typhoid fever, '
            'and intra-abdominal infections (in combination with metronidazole).'
        ),
        'precautions': (
            'Black box warning for tendonitis and tendon rupture (especially Achilles tendon), peripheral neuropathy, '
            'and central nervous system toxicities. Avoid in patients with myasthenia gravis. Maintain adequate hydration.'
        ),
        'contraindications': (
            'Concurrent administration with tizanidine. History of hypersensitivity to quinolones. Myasthenia gravis.'
        ),
        'side_effects': (
            'Nausea, diarrhea, elevated transaminases, dizziness, insomnia, photosensitivity, arthralgia.'
        ),
        'storage_requirements': (
            'Store below 25°C. Protect oral tablets and IV infusions from light and moisture.'
        ),
        'regulatory_schedule': 'Schedule H (Prescription Only)',
        'pregnancy_lactation': 'Avoid during pregnancy and breastfeeding unless no safe alternative is available due to risk of cartilage damage.',
    },
    'metronidazole': {
        'display_name': 'Metronidazole',
        'generic_name': 'Metronidazole',
        'brand_names': ['Flagyl', 'Metrogyl', 'Aldezole'],
        'category': 'Nitroimidazole Antimicrobial / Antiprotozoal',
        'therapeutic_class': 'Synthetic nitroimidazole',
        'common_uses': (
            'Anaerobic bacterial infections (intra-abdominal, pelvic, brain abscess), amoebiasis, giardiasis, '
            'trichomoniasis, pseudomembranous colitis (C. difficile), and surgical prophylaxis.'
        ),
        'precautions': (
            'Strictly avoid alcohol during therapy and for at least 48 hours following completion due to severe disulfiram-like reactions. '
            'Monitor complete blood count and neurological status during prolonged courses.'
        ),
        'contraindications': (
            'First trimester of pregnancy in trichomoniasis. Hypersensitivity to nitroimidazoles. Co-administration with disulfiram.'
        ),
        'side_effects': (
            'Metallic taste in mouth, dark or red-brown urine, nausea, anorexia, peripheral neuropathy, reversible encephalopathy (rare).'
        ),
        'storage_requirements': (
            'Store below 25°C. Protect IV infusions and oral preparations from light.'
        ),
        'regulatory_schedule': 'Schedule H (Prescription Only)',
        'pregnancy_lactation': 'Contraindicated in first trimester; caution in second/third trimesters.',
    },
    'ibuprofen': {
        'display_name': 'Ibuprofen',
        'generic_name': 'Ibuprofen',
        'brand_names': ['Brufen', 'Advil', 'Motrin', 'Ibugesic'],
        'category': 'Non-Steroidal Anti-Inflammatory Drug (NSAID)',
        'therapeutic_class': 'Propionic acid derivative NSAID',
        'common_uses': (
            'Inflammatory disorders (rheumatoid arthritis, osteoarthritis), dental pain, dysmenorrhea, '
            'postoperative analgesia, and pyrexia.'
        ),
        'precautions': (
            'Increases risk of serious gastrointestinal ulceration, bleeding, and perforation. Increases cardiovascular thrombotic risk, '
            'myocardial infarction, and stroke. Monitor renal function in dehydration or renal insufficiency.'
        ),
        'contraindications': (
            'Active gastrointestinal ulceration or bleeding. Severe heart failure, severe renal or hepatic impairment. '
            'Third trimester of pregnancy (causes premature closure of ductus arteriosus).'
        ),
        'side_effects': (
            'Dyspepsia, heartburn, epigastric pain, fluid retention, hypertension, elevated serum creatinine, headache.'
        ),
        'storage_requirements': (
            'Store below 25°C in a dry place. Keep container tightly closed.'
        ),
        'regulatory_schedule': 'General / OTC (low doses); Schedule H for high doses or combination formulations.',
        'pregnancy_lactation': 'Contraindicated in 3rd trimester; avoid in 1st/2nd trimesters unless clinically essential.',
    },
    'diclofenac': {
        'display_name': 'Diclofenac Sodium / Potassium',
        'generic_name': 'Diclofenac',
        'brand_names': ['Voveran', 'Voltaren', 'Dynapar', 'Diclogesic'],
        'category': 'Non-Steroidal Anti-Inflammatory Drug (NSAID)',
        'therapeutic_class': 'Phenylacetic acid derivative NSAID',
        'common_uses': (
            'Acute acute musculoskeletal pain, renal colic, gout attacks, osteoarthritis, rheumatoid arthritis, '
            'and post-traumatic or post-surgical inflammation.'
        ),
        'precautions': (
            'Highest cardiovascular risk among standard NSAIDs; use lowest effective dose for shortest duration. '
            'Monitor blood pressure and renal function. Co-prescribe gastroprotection (PPI) in high-risk patients.'
        ),
        'contraindications': (
            'Established ischaemic heart disease, peripheral arterial disease, cerebrovascular disease, or congestive heart failure. '
            'Active GI bleeding, ulceration, or severe asthma attacks induced by aspirin/NSAIDs.'
        ),
        'side_effects': (
            'Epigastric distress, peptic ulcer, elevated liver transaminases, peripheral edema, headache, dizziness.'
        ),
        'storage_requirements': (
            'Store below 25°C protected from moisture and direct light.'
        ),
        'regulatory_schedule': 'Schedule H (Prescription Only)',
        'pregnancy_lactation': 'Contraindicated in 3rd trimester; use with caution during lactation.',
    },
    'tramadol': {
        'display_name': 'Tramadol Hydrochloride',
        'generic_name': 'Tramadol HCl',
        'brand_names': ['Tramazac', 'Ultram', 'Supridol', 'Contramal'],
        'category': 'Opioid Analgesic / Controlled Substance',
        'therapeutic_class': 'Centrally acting synthetic opioid & SNRI',
        'common_uses': (
            'Management of moderate to moderately severe acute and chronic pain where non-opioids provide inadequate analgesia.'
        ),
        'precautions': (
            'Controlled drug under Schedule H1. Running register and prescription audit mandatory. '
            'Risk of seizure in patients with epilepsy or taking SSRIs/SNRIs (serotonin syndrome). '
            'Risk of respiratory depression and dependence.'
        ),
        'contraindications': (
            'Acute intoxication with alcohol, hypnotics, opioids, or psychotropic agents. Concurrent or recent (within 14 days) MAOI therapy. '
            'Uncontrolled epilepsy.'
        ),
        'side_effects': (
            'Nausea, constipation, dizziness, somnolence, sweating, dry mouth, headache, pruritus, confusion.'
        ),
        'storage_requirements': (
            'Store below 25°C in a locked controlled-substances cabinet. Audit physical counts daily.'
        ),
        'regulatory_schedule': 'Schedule H1 (Prescription & Controlled Running Register Mandatory)',
        'pregnancy_lactation': 'Crosses placenta; contraindicated in pregnancy and breastfeeding due to neonatal respiratory depression risks.',
    },
    'morphine': {
        'display_name': 'Morphine Sulfate',
        'generic_name': 'Morphine Sulfate',
        'brand_names': ['MS Contin', 'Morphitroy', 'Kadian', 'Oramorph'],
        'category': 'Narcotic Analgesic (NDPS / Schedule X)',
        'therapeutic_class': 'Natural opioid alkaloid (Mu-opioid receptor agonist)',
        'common_uses': (
            'Severe acute pain (myocardial infarction, severe burns, major trauma, surgical pain) and palliative cancer pain management.'
        ),
        'precautions': (
            'Strict narcotic control under NDPS regulations. Double-signoff required for dispensing and administration. '
            'Monitor respiratory rate closely. Naloxone must be immediately available as reversal agent.'
        ),
        'contraindications': (
            'Severe respiratory depression, acute asthma, paralytic ileus, raised intracranial pressure, pheochromocytoma.'
        ),
        'side_effects': (
            'Respiratory depression, profound sedation, severe constipation, nausea/vomiting, miosis, hypotension, physical dependence.'
        ),
        'storage_requirements': (
            'Store strictly in a double-locked Narcotic & NDPS safe with dual-pharmacist biometric or key custody.'
        ),
        'regulatory_schedule': 'Narcotic / NDPS (Special Controlled Registry Required)',
        'pregnancy_lactation': 'Restricted strictly to life-threatening indications under intensive hospital care.',
    },
    'metformin': {
        'display_name': 'Metformin Hydrochloride',
        'generic_name': 'Metformin HCl',
        'brand_names': ['Glycomet', 'Glucophage', 'Obimet', 'Riomet'],
        'category': 'Oral Hypoglycemic / Antidiabetic',
        'therapeutic_class': 'Biguanide',
        'common_uses': (
            'First-line pharmacological management of Type 2 Diabetes Mellitus, particularly in overweight patients; '
            'polycystic ovary syndrome (PCOS).'
        ),
        'precautions': (
            'Rare but life-threatening risk of lactic acidosis. Monitor eGFR regularly; discontinue if eGFR falls below 30 mL/min. '
            'Temporarily withhold prior to iodinated radiocontrast procedures and major surgery.'
        ),
        'contraindications': (
            'Severe renal failure (eGFR < 30 mL/min). Acute metabolic acidosis, diabetic ketoacidosis, severe hypoxemia, or shock.'
        ),
        'side_effects': (
            'Gastrointestinal symptoms (diarrhea, nausea, vomiting, abdominal bloating, metallic taste) — usually transient. '
            'Long-term use can reduce Vitamin B12 absorption.'
        ),
        'storage_requirements': (
            'Store below 25°C in a dry place. Protect from moisture and direct sunlight.'
        ),
        'regulatory_schedule': 'Schedule H (Prescription Only)',
        'pregnancy_lactation': 'Acceptable in gestational diabetes under specialist endocrinologist supervision.',
    },
    'glibenclamide': {
        'display_name': 'Glibenclamide (Glyburide)',
        'generic_name': 'Glibenclamide',
        'brand_names': ['Daonil', 'Euglucon', 'Glynase'],
        'category': 'Oral Hypoglycemic / Antidiabetic',
        'therapeutic_class': 'Second-generation sulfonylurea',
        'common_uses': (
            'Type 2 diabetes mellitus when dietary modification and exercise are insufficient.'
        ),
        'precautions': (
            'High risk of severe and prolonged hypoglycemia, especially in elderly or renal-impaired patients. '
            'Ensure meals are not missed following administration.'
        ),
        'contraindications': (
            'Type 1 diabetes mellitus, diabetic ketoacidosis, severe hepatic or renal impairment.'
        ),
        'side_effects': (
            'Hypoglycemia, weight gain, allergic skin reactions, mild gastrointestinal disturbances.'
        ),
        'storage_requirements': (
            'Store below 25°C in a dry location.'
        ),
        'regulatory_schedule': 'Schedule H (Prescription Only)',
        'pregnancy_lactation': 'Avoid in pregnancy; insulin is preferred.'
    },
    'insulin': {
        'display_name': 'Insulin Glargine',
        'generic_name': 'Insulin Glargine',
        'brand_names': ['Lantus', 'Basaglar', 'Toujeo'],
        'category': 'Antidiabetic / Long-Acting Insulin Analogue',
        'therapeutic_class': 'Recombinant DNA human insulin analogue',
        'common_uses': (
            'Treatment of diabetes mellitus in adults, adolescents, and children aged 2 years and above requiring long-acting basal insulin.'
        ),
        'precautions': (
            'Never freeze. Rotate injection sites within chosen area to reduce risk of lipodystrophy. '
            'Educate patient on hypoglycemia recognition and management.'
        ),
        'contraindications': (
            'Hypersensitivity to insulin glargine. During episodes of acute hypoglycemia.'
        ),
        'side_effects': (
            'Hypoglycemia, lipodystrophy at injection site, local injection reactions, peripheral edema.'
        ),
        'storage_requirements': (
            'Cold chain mandatory: Store unopened vials/pens at 2°C to 8°C (Do Not Freeze). '
            'In-use vials/pens can be kept at room temperature (below 30°C) for up to 28 days.'
        ),
        'regulatory_schedule': 'Schedule H (Prescription Only)',
        'pregnancy_lactation': 'Can be used during pregnancy if clinically indicated under endocrinology guidance.',
    },
    'amlodipine': {
        'display_name': 'Amlodipine Besylate',
        'generic_name': 'Amlodipine Besylate',
        'brand_names': ['Amlong', 'Norvasc', 'Stamlo'],
        'category': 'Antihypertensive / Calcium Channel Blocker',
        'therapeutic_class': 'Dihydropyridine calcium channel blocker',
        'common_uses': (
            'Essential hypertension, chronic stable angina pectoris, and vasospastic (Prinzmetal\'s) angina.'
        ),
        'precautions': (
            'Use with caution in severe aortic stenosis, severe hepatic impairment, and heart failure.'
        ),
        'contraindications': (
            'Severe hypotension, cardiogenic shock, unstable angina (excluding vasospastic).'
        ),
        'side_effects': (
            'Peripheral edema (ankle swelling), flushing, palpitations, dizziness, headache, fatigue.'
        ),
        'storage_requirements': (
            'Store below 25°C. Protect from moisture and light.'
        ),
        'regulatory_schedule': 'Schedule H (Prescription Only)',
        'pregnancy_lactation': 'Use only if benefits outweigh risks; alternative antihypertensives usually preferred in pregnancy.',
    },
    'omeprazole': {
        'display_name': 'Omeprazole',
        'generic_name': 'Omeprazole Magnesium',
        'brand_names': ['Omez', 'Prilosec', 'Omecip'],
        'category': 'Gastrointestinal / Proton Pump Inhibitor (PPI)',
        'therapeutic_class': 'Substituted benzimidazole PPI',
        'common_uses': (
            'Gastroesophageal reflux disease (GERD), erosive esophagitis, duodenal/gastric ulcers, Zollinger-Ellison syndrome, '
            'and H. pylori eradication (in triple therapy).'
        ),
        'precautions': (
            'Long-term use may cause hypomagnesemia, increased risk of Clostridioides difficile diarrhea, '
            'and increased risk of osteoporotic fractures. May decrease absorption of Vitamin B12.'
        ),
        'contraindications': (
            'Concomitant use with nelfinavir or rilpivirine. Hypersensitivity to PPIs.'
        ),
        'side_effects': (
            'Headache, abdominal pain, diarrhea, constipation, flatulence, nausea.'
        ),
        'storage_requirements': (
            'Store below 25°C in a tightly closed container. Protect from moisture.'
        ),
        'regulatory_schedule': 'General / OTC (low strengths); Schedule H for prescription doses.',
        'pregnancy_lactation': 'Considered compatible with pregnancy and breastfeeding when indicated.',
    },
    'pantoprazole': {
        'display_name': 'Pantoprazole Sodium',
        'generic_name': 'Pantoprazole Sodium',
        'brand_names': ['Pan 40', 'Pantocid', 'Protonix'],
        'category': 'Gastrointestinal / Proton Pump Inhibitor (PPI)',
        'therapeutic_class': 'Substituted benzimidazole PPI',
        'common_uses': (
            'Short-term treatment of erosive esophagitis associated with GERD, peptic ulcer healing, '
            'and prevention of NSAID-induced gastric ulcers.'
        ),
        'precautions': (
            'Evaluate for gastric malignancy before initiating therapy for gastric ulcer. '
            'Low CYP2C19 drug interaction profile compared to omeprazole.'
        ),
        'contraindications': (
            'Severe hypersensitivity to pantoprazole or other substituted benzimidazoles.'
        ),
        'side_effects': (
            'Benign fundic gland polyps (long term), headache, diarrhea, nausea, dizziness, arthralgia.'
        ),
        'storage_requirements': (
            'Store below 25°C protected from light and moisture.'
        ),
        'regulatory_schedule': 'Schedule H (Prescription Only)',
        'pregnancy_lactation': 'Use only if clearly needed during pregnancy.',
    },
    'cetirizine': {
        'display_name': 'Cetirizine Hydrochloride',
        'generic_name': 'Cetirizine HCl',
        'brand_names': ['Cetzine', 'Zyrtec', 'Alerid', 'Okacet'],
        'category': 'Antihistamine',
        'therapeutic_class': 'Second-generation piperazine H1 receptor antagonist',
        'common_uses': (
            'Relief of nasal and ocular symptoms of seasonal and perennial allergic rhinitis, '
            'chronic idiopathic urticaria, and allergic conjunctivitis.'
        ),
        'precautions': (
            'May cause mild sedation in sensitive individuals; caution when driving or operating machinery. '
            'Adjust dose in moderate to severe renal impairment.'
        ),
        'contraindications': (
            'Severe renal impairment (creatinine clearance < 10 mL/min). Hypersensitivity to hydroxyzine or cetirizine.'
        ),
        'side_effects': (
            'Mild somnolence, fatigue, dry mouth, headache, dizziness.'
        ),
        'storage_requirements': (
            'Store below 25°C in a dry place.'
        ),
        'regulatory_schedule': 'General / OTC',
        'pregnancy_lactation': 'Compatible with pregnancy and lactation at standard doses.',
    },
    'salbutamol': {
        'display_name': 'Salbutamol (Albuterol) Inhaler',
        'generic_name': 'Salbutamol Sulfate',
        'brand_names': ['Asthalin', 'Ventolin', 'Aerolin'],
        'category': 'Bronchodilator / Respiratory',
        'therapeutic_class': 'Short-acting selective beta-2 adrenergic agonist (SABA)',
        'common_uses': (
            'Relief and prevention of acute bronchospasm in bronchial asthma, chronic bronchitis, '
            'and exercise-induced bronchoconstriction.'
        ),
        'precautions': (
            'Excessive use indicates poorly controlled asthma requiring corticosteroid reassessment. '
            'Caution in cardiovascular disorders (arrhythmias, coronary insufficiency), hyperthyroidism, diabetes.'
        ),
        'contraindications': (
            'Hypersensitivity to salbutamol. Non-IV formulations are contraindicated in threatened abortion.'
        ),
        'side_effects': (
            'Fine skeletal muscle tremor (especially hands), palpitations, tachycardia, nervousness, hypokalemia (high doses).'
        ),
        'storage_requirements': (
            'Store below 30°C. Protect from frost and direct sunlight. Pressurized canister — do not puncture or burn.'
        ),
        'regulatory_schedule': 'Schedule H (Prescription Only)',
        'pregnancy_lactation': 'Widely used in pregnancy for acute asthma; benefits typically outweigh risks.',
    },
    'atorvastatin': {
        'display_name': 'Atorvastatin Calcium',
        'generic_name': 'Atorvastatin Calcium',
        'brand_names': ['Atorva', 'Lipitor', 'Storvas'],
        'category': 'Cardiovascular / Lipid-Lowering Agent',
        'therapeutic_class': 'HMG-CoA reductase inhibitor (Statin)',
        'common_uses': (
            'Reduction of elevated total cholesterol, LDL-C, and triglycerides in primary hypercholesterolemia; '
            'prevention of cardiovascular events in high-risk patients.'
        ),
        'precautions': (
            'Perform baseline liver function tests. Instruct patients to report unexplained muscle pain or weakness (risk of rhabdomyolysis). '
            'Avoid substantial grapefruit juice intake.'
        ),
        'contraindications': (
            'Active liver disease or unexplained persistent elevations in serum transaminases. Pregnancy and breastfeeding.'
        ),
        'side_effects': (
            'Myalgia, arthralgia, elevated transaminases, nasopharyngitis, dyspepsia, headache.'
        ),
        'storage_requirements': (
            'Store below 25°C in original packaging.'
        ),
        'regulatory_schedule': 'Schedule H (Prescription Only)',
        'pregnancy_lactation': 'Contraindicated in pregnancy and nursing mothers.',
    },
    'aspirin': {
        'display_name': 'Aspirin (Acetylsalicylic Acid)',
        'generic_name': 'Acetylsalicylic Acid',
        'brand_names': ['Ecosprin', 'Disprin', 'Bayer Aspirin'],
        'category': 'Antiplatelet / Analgesic',
        'therapeutic_class': 'Salicylate / Cyclooxygenase-1 inhibitor',
        'common_uses': (
            'Secondary prevention of cardiovascular and cerebrovascular events (low-dose 75–150 mg); '
            'acute treatment of mild-to-moderate pain and fever (high-dose).'
        ),
        'precautions': (
            'Never administer to children or adolescents with viral infections due to risk of Reye\'s syndrome. '
            'Monitor for gastrointestinal bleeding. Discontinue before elective surgery if advised by surgical team.'
        ),
        'contraindications': (
            'Active peptic ulceration, bleeding diathesis, hemophilia, aspirin-induced asthma, third trimester of pregnancy.'
        ),
        'side_effects': (
            'Gastric irritation, occult GI blood loss, tinnitus (high doses), prolonged bleeding time.'
        ),
        'storage_requirements': (
            'Store below 25°C in a dry place. Discard if vinegar odor develops.'
        ),
        'regulatory_schedule': 'General / OTC',
        'pregnancy_lactation': 'Avoid high doses; low-dose under obstetric supervision.',
    },
}


def get_medicine_knowledge(medicine_name_or_keyword):
    """
    Looks up clinical pharmacology monograph for a given query text.
    Returns dict with knowledge data or None.
    """
    if not medicine_name_or_keyword:
        return None
        
    query = medicine_name_or_keyword.lower().strip()
    
    # 1. Exact match on dictionary key
    for key, data in CLINICAL_KNOWLEDGE_BASE.items():
        if key in query:
            return data
            
    # 2. Match on brand names
    for key, data in CLINICAL_KNOWLEDGE_BASE.items():
        for brand in data.get('brand_names', []):
            if brand.lower() in query:
                return data

    return None
