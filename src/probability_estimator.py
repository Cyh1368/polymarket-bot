"""Use Claude to estimate event probabilities from news evidence."""

import json
import logging
from datetime import datetime

import anthropic

from .models import NewsArticle, Prediction
from .news_fetcher import format_articles_for_prompt

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"
TIMEOUT = 30.0
MAX_RETRIES = 3

SYSTEM_PROMPT = """You are a calibrated prediction analyst. You make probabilistic forecasts
that are well-calibrated: when you say 70%, events should resolve YES about 70% of the time.
You are aware of common cognitive biases and actively correct for them."""

ESTIMATION_PROMPT = """Analyze the following question and news evidence, then estimate the probability this event will occur.

QUESTION: {question}
RESOLUTION DATE: {end_date}
CURRENT MARKET PRICE (crowd estimate): {yes_price_pct}%

NEWS EVIDENCE:
{news_text}

Instructions:
1. Apply Bayesian reasoning: start with base rate, update on evidence
2. Consider: news recency, source reliability, black swan risk
3. Watch for: confirmation bias, narrative fallacies, fake/delayed news
4. Return a JSON object with:
   - estimated_probability: float (0.0 to 1.0)
   - confidence: "low" | "medium" | "high"
   - reasoning: string (3-5 sentences)
   - key_evidence: list of 3 most important facts
   - risks: list of 2-3 things that could make you wrong
   - bayesian_prior: float (what's the base rate before news?)

Return ONLY valid JSON, no other text."""


async def estimate_probability(
    market_id: str,
    question: str,
    end_date: datetime,
    yes_price: float,
    articles: list[NewsArticle],
    api_key: str,
) -> Prediction:
    """Call Claude to estimate the probability for a market question.

    Includes retry logic (max 3 attempts) and 30s timeout.
    """
    news_text = format_articles_for_prompt(articles)
    prompt = ESTIMATION_PROMPT.format(
        question=question,
        end_date=end_date.strftime("%Y-%m-%d"),
        yes_price_pct=round(yes_price * 100, 1),
        news_text=news_text,
    )

    client = anthropic.Anthropic(api_key=api_key)

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                timeout=TIMEOUT,
            )

            text = response.content[0].text.strip()
            parsed = _parse_response(text)

            prediction = Prediction(
                market_id=market_id,
                timestamp=datetime.utcnow(),
                claude_probability=parsed["estimated_probability"],
                market_price=yes_price,
                edge=round(parsed["estimated_probability"] - yes_price, 4),
                confidence=parsed["confidence"],
                reasoning=parsed["reasoning"],
                bayesian_prior=parsed["bayesian_prior"],
                key_evidence=parsed.get("key_evidence", []),
                risks=parsed.get("risks", []),
                news_articles=[a.url for a in articles],
                news_quality_score=len(articles),
            )

            logger.info(
                "Estimated %s: prob=%.2f (market=%.2f, edge=%.2f) confidence=%s",
                market_id,
                prediction.claude_probability,
                yes_price,
                prediction.edge,
                prediction.confidence,
            )
            return prediction

        except anthropic.APITimeoutError:
            last_error = f"Timeout on attempt {attempt}/{MAX_RETRIES}"
            logger.warning(last_error)
        except anthropic.APIError as e:
            last_error = f"API error on attempt {attempt}/{MAX_RETRIES}: {e}"
            logger.warning(last_error)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            last_error = f"Parse error on attempt {attempt}/{MAX_RETRIES}: {e}"
            logger.warning(last_error)

    # All retries exhausted — return a low-confidence fallback
    logger.error("All %d attempts failed for %s: %s", MAX_RETRIES, market_id, last_error)
    return Prediction(
        market_id=market_id,
        timestamp=datetime.utcnow(),
        claude_probability=yes_price,  # Default to market consensus
        market_price=yes_price,
        edge=0.0,
        confidence="low",
        reasoning=f"Estimation failed after {MAX_RETRIES} attempts: {last_error}",
        bayesian_prior=yes_price,
        key_evidence=[],
        risks=["Estimation failed — using market consensus as fallback"],
        news_articles=[a.url for a in articles],
        news_quality_score=len(articles),
    )


def _parse_response(text: str) -> dict:
    """Parse and validate Claude's JSON response."""
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    data = json.loads(text)

    # Validate probability range
    prob = float(data["estimated_probability"])
    if not 0.0 <= prob <= 1.0:
        raise ValueError(f"Probability {prob} not in [0.0, 1.0]")
    data["estimated_probability"] = prob

    # Validate confidence
    if data["confidence"] not in ("low", "medium", "high"):
        raise ValueError(f"Invalid confidence: {data['confidence']}")

    # Validate bayesian_prior
    prior = float(data["bayesian_prior"])
    if not 0.0 <= prior <= 1.0:
        raise ValueError(f"Bayesian prior {prior} not in [0.0, 1.0]")
    data["bayesian_prior"] = prior

    return data
