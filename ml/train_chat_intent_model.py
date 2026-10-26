"""
ml/train_chat_intent_model.py
MediVault — Sanalogx AI Assistant Intent Classifier Training & Evaluation Pipeline

Trains a supervised NLP intent classifier on curated pharmacy queries (English & Hinglish).
Evaluates Accuracy, Precision, Recall, F1-score with stratified holdout test split.
Saves model artifacts, vectorizer, and evaluation metadata.
"""

import os
import json
import re
from datetime import datetime

import pandas as pd
import numpy as np
import joblib

from sklearn.model_selection import train_test_split
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, accuracy_score, precision_recall_fscore_support


def clean_text(text):
    if not isinstance(text, str):
        return ""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s\-]', ' ', text)
    text = " ".join(text.split())
    return text


def train_chat_intent_model():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    csv_path = os.path.join(base_dir, "ml", "datasets", "pharmacy_chat_intents.csv")
    models_dir = os.path.join(base_dir, "ml", "models")
    os.makedirs(models_dir, exist_ok=True)

    print(f"Loading Sanalogx AI Assistant intent dataset from: {csv_path}")
    df = pd.read_csv(csv_path)

    df["text"] = df["text"].apply(clean_text)
    df = df[df["text"] != ""].dropna(subset=["intent"])

    X = df["text"]
    y = df["intent"]

    total_samples = len(df)
    unique_classes = sorted(y.unique().tolist())
    print(f"Total dataset samples: {total_samples}")
    print(f"Unique intent classes ({len(unique_classes)}): {unique_classes}")

    # Stratified Train/Test Split (80% Train, 20% Holdout Test)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.20, random_state=42, stratify=y
    )

    print(f"Training samples: {len(X_train)} | Test samples: {len(X_test)}")

    # TF-IDF Feature Extraction with Unigram & Bigram n-grams
    vectorizer = TfidfVectorizer(
        ngram_range=(1, 2),
        min_df=1,
        sublinear_tf=True
    )
    X_train_tfidf = vectorizer.fit_transform(X_train)
    X_test_tfidf = vectorizer.transform(X_test)

    # Supervised Classifier: Multinomial Logistic Regression with Balanced Class Weight
    classifier = LogisticRegression(
        C=10.0,
        max_iter=1000,
        class_weight='balanced',
        random_state=42
    )
    classifier.fit(X_train_tfidf, y_train)

    # Evaluation on Holdout Test Set
    y_pred = classifier.predict(X_test_tfidf)
    acc = accuracy_score(y_test, y_pred)
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_test, y_pred, average="weighted", zero_division=0
    )

    print("\n" + "=" * 64)
    print("        SANALOGX AI ASSISTANT MODEL EVALUATION REPORT        ")
    print("=" * 64)
    print(f"Overall Accuracy : {acc * 100:.2f}%")
    print(f"Weighted Precision: {precision * 100:.2f}%")
    print(f"Weighted Recall   : {recall * 100:.2f}%")
    print(f"Weighted F1-Score : {f1 * 100:.2f}%")
    print("=" * 64 + "\n")
    print(classification_report(y_test, y_pred, zero_division=0))

    # Save Model Artifacts
    model_path = os.path.join(models_dir, "chat_intent_model.joblib")
    vectorizer_path = os.path.join(models_dir, "chat_vectorizer.joblib")
    metadata_path = os.path.join(models_dir, "chat_model_metadata.json")

    joblib.dump(classifier, model_path)
    joblib.dump(vectorizer, vectorizer_path)

    metadata = {
        'model_name': 'SanalogxChatIntentClassifier',
        'algorithm': 'LogisticRegression (Multinomial)',
        'vectorizer': 'TfidfVectorizer (unigram + bigram, sublinear_tf)',
        'version': '1.0.0',
        'trained_at': datetime.now().isoformat(),
        'dataset_file': 'pharmacy_chat_intents.csv',
        'total_samples': total_samples,
        'train_samples': len(X_train),
        'test_samples': len(X_test),
        'num_classes': len(unique_classes),
        'classes': unique_classes,
        'metrics': {
            'accuracy': round(float(acc), 4),
            'precision_weighted': round(float(precision), 4),
            'recall_weighted': round(float(recall), 4),
            'f1_weighted': round(float(f1), 4),
        }
    }

    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2)

    print(f"Trained model saved to      : {model_path}")
    print(f"Trained vectorizer saved to : {vectorizer_path}")
    print(f"Training metadata saved to  : {metadata_path}")
    print("Sanalogx AI Assistant Training Pipeline Completed Successfully!\n")
    return metadata


if __name__ == "__main__":
    train_chat_intent_model()
