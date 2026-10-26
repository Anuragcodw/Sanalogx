import os
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
    text = " ".join(text.split())
    return text


def train_voice_intent_model():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    csv_path = os.path.join(base_dir, "ml", "datasets", "voice_intents.csv")
    models_dir = os.path.join(base_dir, "ml", "models")

    os.makedirs(models_dir, exist_ok=True)

    print(f"Loading MediVoice intent dataset from: {csv_path}")
    df = pd.read_csv(csv_path)

    df["text"] = df["text"].apply(clean_text)
    df = df[df["text"] != ""].dropna(subset=["intent"])

    X = df["text"]
    y = df["intent"]

    print(f"Total dataset samples: {len(df)}")
    print(f"Unique intent classes: {y.nunique()}")

    # Stratified Train/Test split
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.20, random_state=42, stratify=y
    )

    # TF-IDF Vectorizer
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), min_df=1)
    X_train_tfidf = vectorizer.fit_transform(X_train)
    X_test_tfidf = vectorizer.transform(X_test)

    # Logistic Regression Classifier
    classifier = LogisticRegression(C=5.0, max_iter=500, random_state=42)
    classifier.fit(X_train_tfidf, y_train)

    # Evaluation
    y_pred = classifier.predict(X_test_tfidf)
    acc = accuracy_score(y_test, y_pred)
    precision, recall, f1, _ = precision_recall_fscore_support(y_test, y_pred, average="weighted")

    print("\n========================================================")
    print("           MEDIVOICE AI MODEL EVALUATION REPORT        ")
    print("========================================================")
    print(f"Accuracy : {acc * 100:.2f}%")
    print(f"Precision: {precision * 100:.2f}%")
    print(f"Recall   : {recall * 100:.2f}%")
    print(f"F1 Score : {f1 * 100:.2f}%")
    print("========================================================\n")
    print(classification_report(y_test, y_pred, zero_division=0))

    # Save joblib model artifacts
    model_path = os.path.join(models_dir, "voice_intent_model.joblib")
    vectorizer_path = os.path.join(models_dir, "voice_vectorizer.joblib")

    joblib.dump(classifier, model_path)
    joblib.dump(vectorizer, vectorizer_path)

    print(f"Trained model saved to     : {model_path}")
    print(f"Trained vectorizer saved to: {vectorizer_path}")
    print("MediVoice AI Training Pipeline Completed Successfully!\n")


if __name__ == "__main__":
    train_voice_intent_model()
