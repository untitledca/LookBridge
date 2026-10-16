import Foundation

enum AppSupportLinks {
    struct Option: Identifiable {
        let amount: Int
        let checkoutURL: URL
        var id: Int { amount }
        var label: String { "$\(amount) USD" }
    }

    /// The same optional, one-time QuickBooks checkouts used at atomfilmlab.com/apps.
    /// Opening a URL does not submit a payment; confirmation happens in the browser.
    static let options = [
        Option(amount: 5, checkoutURL: URL(string: "https://connect.intuit.com/pay/AtomFilmLabInc/scs-v1-ea4fa06c85294c0bba3e1bc44c31841b58a15d02de6747958704525045c9112d95a87fb2e2b541449d7f00193fcf05b0?locale=EN_US&cta=saveandcopylink")!),
        Option(amount: 10, checkoutURL: URL(string: "https://connect.intuit.com/pay/AtomFilmLabInc/scs-v1-16dd11c181c448d88fb730fff2248be83f572df0d7434f148156c68d7ec6c05edc4f8a6ce74b48218231421dc1f05cce-1?locale=EN_US&cta=saveandcopylink")!),
        Option(amount: 25, checkoutURL: URL(string: "https://connect.intuit.com/pay/AtomFilmLabInc/scs-v1-3dbf78b790fc4ae69f42a22f3f60b86a9a8c54d7e98f4e2fa9f749b01b5ecc2632ab2bd437b1489187f33fda7703649d-0?locale=EN_US&cta=saveandcopylink")!)
    ]
}
