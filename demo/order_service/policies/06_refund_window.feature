Feature: refund SLA

  Scenario: a cancelled order is refunded within the window
    When an order is "cancelled"
    Then an order is "refunded" within "5" seconds
