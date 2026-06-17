---
trigger: always_on
---

# Python Code Quality & Documentation Standards

## 1. Compliance & Linting
- **Standard:** Adhere strictly to **PEP 8** style guidelines.
- **Linting:** Ensure code passes `flake8` and `black` formatting standards. 
- **Consistency:** Use explicit variable names over short abbreviations (e.g., `user_account_id` instead of `uaid`).

## 2. Docstrings (Google Style)
Every function, class, and module must include a comprehensive docstring using the following structure:

### Summary
A brief one-line description of the purpose.

### Parameters / Args
- List every parameter with its expected type.
- **The "Why":** Include a brief explanation of why this parameter is necessary or how it influences the logic, especially for optional flags.

### Returns
- Specify the return type.
- Describe the data being returned and its structure (e.g., "A dictionary containing the validated transaction record").

### Raises
- Document any exceptions that are explicitly raised within the block.

## 3. Implementation Example
```python
def calculate_risk_score(account_balance: float, transaction_history: list) -> float:
    """
    Calculates a dynamic risk score based on liquidity and history.

    Args:
        account_balance (float): The current liquid assets. Used to determine
            the baseline buffer for the risk threshold.
        transaction_history (list): A list of recent transaction objects. 
            Analyzed to identify volatility patterns in spending.

    Returns:
        float: A normalized score between 0.0 and 1.0 representing risk level.
    """
    pass