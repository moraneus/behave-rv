Feature: cancellation safety

  Scenario: an order must never be cancelled
    Then an order is "cancelled" never happens
