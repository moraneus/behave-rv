Feature: payment SLA

  Scenario: an authorized order is paid within the window
    When an order is "authorized"
    Then an order is "paid" within "10" seconds
