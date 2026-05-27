from datasets import Dataset
from ragas import evaluate
from ragas.metrics import (
    faithfulness,
    answer_relevancy,
    context_recall,
    context_precision,
)

# Simple sample data
data = {
    "question": [
        "What is the capital of France?",
        "Who invented the telephone?",
        "What is photosynthesis?",
    ],
    "answer": [
        "The capital of France is Paris.",
        "Alexander Graham Bell invented the telephone.",
        "Photosynthesis is the process by which plants convert sunlight into food.",
    ],
    "contexts": [
        ["Paris is the capital and most populous city of France."],
        ["Alexander Graham Bell is credited with inventing the telephone in 1876."],
        ["Photosynthesis is a process used by plants to convert light energy into chemical energy."],
    ],
    "ground_truth": [
        "Paris",
        "Alexander Graham Bell",
        "Photosynthesis converts sunlight into chemical energy in plants.",
    ],
}

dataset = Dataset.from_dict(data)

results = evaluate(
    dataset=dataset,
    metrics=[
        faithfulness,
        answer_relevancy,
        context_recall,
        context_precision,
    ],
)

print(results)
print(results.to_pandas())
