Feature: sustained payment

  Scenario: an order stays paid once it is authorized
    Then an order is "paid" since an order is "authorized"
