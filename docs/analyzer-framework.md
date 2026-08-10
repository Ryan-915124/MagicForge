# Magic Theory Analyzer v0.2

The analyzer is a structured evaluation pipeline, not a free-form prompt wrapper.

## Dimensions and rubric

| Dimension | Required criteria |
|---|---|
| Effect strength | clarity, impossibility gap, emotional stakes, progression |
| Method concealment | method-effect distance, naturalness, layering, vulnerability control |
| Psychological principles | attention management, assumption design, memory management, choice architecture |
| Audience experience | initial comprehension, conviction, emotional arc, aftermath |
| Performance design | motivation, pacing, staging, practicality |

GLM returns a validated assessment for every criterion: an anchored score from 1 to 5, rationale, and zero or more retrieved source numbers. Code rejects missing criteria and source numbers that do not exist in the retrieval result. It then calculates each dimension score and the overall score; GLM cannot directly set aggregate scores.

Unknown methods must remain explicit assumptions. A retrieved passage can be cited only by its provided source number. Each dimension also carries risks and actionable recommendations.

This framework separates four responsibilities:

1. Qdrant retrieves relevant domain evidence.
2. GLM evaluates fixed, typed criteria.
3. Pydantic validates completeness and structure.
4. Framework code verifies citations and calculates scores.
