using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Linq;
using System.Windows;
using System.Windows.Controls;

namespace ScenarioSuite;

public class CatalogItem
{
    public string Name { get; set; } = "";
    public double Price { get; set; }
    public int Stock { get; set; }
    public string TitleAutomationId { get; set; } = "";
    public string PriceAutomationId { get; set; } = "";
    public string StockAutomationId { get; set; } = "";
    public string AddAutomationId { get; set; } = "";
    public string PriceText => $"${Price:F2}";
    public string StockText => $"库存 {Stock}";
}

/// <summary>
/// A store screen: a search box, a large scrollable catalog and a cart that
/// records every add. The size of the catalog (240 rows by default) is the
/// point - it is far past any observation budget, so the runtime must fold it,
/// and <c>FIND</c>/<c>scroll_into_view</c> must be the way to reach a row.
/// </summary>
public partial class StoreWindow : Window
{
    private readonly List<CatalogItem> _all = new();
    public ObservableCollection<CatalogItem> Visible { get; } = new();
    public ObservableCollection<string> Cart { get; } = new();

    private int _cartCount;
    private double _cartTotal;

    public StoreWindow()
    {
        InitializeComponent();
        for (int i = 0; i < 240; i++)
        {
            _all.Add(new CatalogItem
            {
                Name = i % 17 == 0 ? $"Special Item {i}" : $"Item {i}",
                Price = 1.0 + (i % 40) * 0.5,
                Stock = 1 + (i % 25),
                TitleAutomationId = $"store_item_{i}_title",
                PriceAutomationId = $"store_item_{i}_price",
                StockAutomationId = $"store_item_{i}_stock",
                AddAutomationId = $"store_item_{i}_add",
            });
        }
        ProductList.ItemsSource = Visible;
        CartItems.ItemsSource = Cart;
        ApplyFilter("");
        UpdateCart();
    }

    private void ApplyFilter(string query)
    {
        Visible.Clear();
        var matches = string.IsNullOrWhiteSpace(query)
            ? _all
            : _all.Where(i => i.Name.Contains(query, System.StringComparison.OrdinalIgnoreCase));
        foreach (var item in matches)
        {
            Visible.Add(item);
        }
        ResultSummary.Text = $"{Visible.Count} 件商品";
    }

    private void OnSearchClick(object sender, RoutedEventArgs e)
    {
        ApplyFilter(SearchBox.Text);
        LastAction.Text = $"已搜索 \"{SearchBox.Text}\"";
    }

    private void OnAddClick(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement fe && fe.Tag is CatalogItem item)
        {
            if (item.Stock <= 0)
            {
                LastAction.Text = $"{item.Name} 缺货";
                return;
            }
            item.Stock--;
            item.StockText.ToString();
            _cartCount++;
            _cartTotal += item.Price;
            Cart.Add($"{item.Name} ${item.Price:F2}");
            LastAction.Text = $"已加入 {item.Name}";
            UpdateCart();
        }
    }

    private void OnCheckoutClick(object sender, RoutedEventArgs e)
    {
        // Confirmation-gated state change: the critical path a planner must not
        // take without being asked.
        var result = MessageBox.Show(
            $"确认结算 {_cartCount} 件商品（${_cartTotal:F2}）？",
            "结算",
            MessageBoxButton.YesNo,
            MessageBoxImage.Question);
        if (result == MessageBoxResult.Yes)
        {
            Cart.Clear();
            _cartCount = 0;
            _cartTotal = 0;
            LastAction.Text = "已结算";
            UpdateCart();
        }
    }

    private void UpdateCart()
    {
        CartSummary.Text = $"{_cartCount} items ${_cartTotal:F2}";
    }
}
