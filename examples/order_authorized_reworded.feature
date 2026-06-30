Feature: payment safety (reworded)

  # Same policy as order_authorized.feature, but every step is phrased
  # differently. Both phrasings resolve to step_id order.status.is, so this
  # compiles and runs identically.
  Scenario: an order may only be paid after it was authorized
    When the order reaches "paid"
    Then the order reaches "authorized" before
