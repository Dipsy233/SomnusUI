using System.Collections.ObjectModel;
using System.Windows;

namespace RestaurantDemo;

public class Product
{
    public string Name { get; set; } = "";
    public double Price { get; set; }
    public string TitleAutomationId { get; set; } = "";
    public string PriceAutomationId { get; set; } = "";
    public string AddAutomationId { get; set; } = "";
    public string PriceText => $"${Price:F2}";
}

public partial class MainWindow : Window
{
    public ObservableCollection<Product> Products { get; } = new();
    public int CartCount { get; private set; }
    public double CartTotal { get; private set; }

    public MainWindow()
    {
        InitializeComponent();

        var catalog = new (string Name, double Price)[]
        {
            ("Big Mac", 5.99),
            ("Fries", 2.49),
            ("Cola", 1.99),
            ("McChicken", 4.59),
            ("Apple Pie", 1.29),
            ("Milkshake", 3.49),
        };

        for (int i = 0; i < catalog.Length; i++)
        {
            var (name, price) = catalog[i];
            Products.Add(new Product
            {
                Name = name,
                Price = price,
                TitleAutomationId = $"product_{i}_title",
                PriceAutomationId = $"product_{i}_price",
                AddAutomationId = $"product_{i}_add",
            });
        }

        ProductList.ItemsSource = Products;
        UpdateCart();
    }

    private void OnAddClick(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement fe && fe.Tag is Product product)
        {
            CartCount++;
            CartTotal += product.Price;
            UpdateCart();
        }
    }

    private void OnOpenCartClick(object sender, RoutedEventArgs e)
    {
        MessageBox.Show(
            $"Cart: {CartCount} items, ${CartTotal:F2}",
            "Cart",
            MessageBoxButton.OK,
            MessageBoxImage.Information);
    }

    private void OnCheckoutClick(object sender, RoutedEventArgs e)
    {
        // Checkout is deliberately a confirmation-gated, state-changing action.
        var result = MessageBox.Show(
            $"Confirm checkout for {CartCount} items (${CartTotal:F2})?",
            "Checkout",
            MessageBoxButton.YesNo,
            MessageBoxImage.Question);
        if (result == MessageBoxResult.Yes)
        {
            CartCount = 0;
            CartTotal = 0;
            UpdateCart();
        }
    }

    private void UpdateCart()
    {
        // The single cart text node carries both the count and the total, which
        // matches the pack's `cart.count`/`cart.total` regex extraction.
        CartSummary.Text = $"{CartCount} items ${CartTotal:F2}";
    }
}
