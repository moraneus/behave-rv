Feature: refund discipline

  Scenario: a refund requires a prior cancellation
    When an order is "refunded"
    Then an order is "cancelled" before
