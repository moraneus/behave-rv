Feature: cancellation safety

  Scenario: a cancelled order is never shipped
    Given an order is "cancelled"
    Then an order is "shipped" never happens
