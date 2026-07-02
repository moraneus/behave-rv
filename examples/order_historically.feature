Feature: validity invariant

  Scenario: every event for an order must be a valid event
    Then an order is "valid" always holds
