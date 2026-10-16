using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Controls;

namespace ScenarioSuite;

public class TreeNode
{
    public string Name { get; set; } = "";
    public string AutomationId { get; set; } = "";
    public List<TreeNode> Children { get; } = new();
}

/// <summary>
/// A deep, expandable tree. Each node has several children, so level 1 is a few
/// nodes, level 3 is hundreds and level 5 is thousands. The collapsed form is
/// what the observation budget can afford; expanding one node at a time is how
/// an agent descends without paying for the whole tree.
/// </summary>
public partial class TreeWindow : Window
{
    private readonly TreeNode _root = new() { Name = "根目录", AutomationId = "tree_root_node" };

    public TreeWindow()
    {
        InitializeComponent();
        for (int a = 0; a < 6; a++)
        {
            var l1 = new TreeNode { Name = $"项目 {a}", AutomationId = $"tree_a{a}" };
            for (int b = 0; b < 6; b++)
            {
                var l2 = new TreeNode { Name = $"模块 {a}.{b}", AutomationId = $"tree_a{a}_b{b}" };
                for (int c = 0; c < 6; c++)
                {
                    var l3 = new TreeNode
                    {
                        Name = $"文件 {a}.{b}.{c}",
                        AutomationId = $"tree_a{a}_b{b}_c{c}",
                    };
                    for (int d = 0; d < 4; d++)
                    {
                        l3.Children.Add(new TreeNode
                        {
                            Name = $"条目 {a}.{b}.{c}.{d}",
                            AutomationId = $"tree_a{a}_b{b}_c{c}_d{d}",
                        });
                    }
                    l2.Children.Add(l3);
                }
                l1.Children.Add(l2);
            }
            _root.Children.Add(l1);
        }

        Tree.ItemsSource = new[] { _root };
        Tree.SelectedItemChanged += (_, e) =>
        {
            SelectionInfo.Text = e.NewValue is TreeNode n ? $"选中 {n.Name}" : "";
        };
        UpdateStatus();
    }

    private void OnExpandAll(object sender, RoutedEventArgs e)
    {
        foreach (var node in Enumerate(_root))
        {
            SetExpanded(node, true);
        }
        UpdateStatus();
    }

    private void OnCollapseAll(object sender, RoutedEventArgs e)
    {
        foreach (var node in Enumerate(_root))
        {
            SetExpanded(node, false);
        }
        UpdateStatus();
    }

    private static IEnumerable<TreeNode> Enumerate(TreeNode node)
    {
        yield return node;
        foreach (var child in node.Children)
        {
            foreach (var descendant in Enumerate(child))
            {
                yield return descendant;
            }
        }
    }

    private void SetExpanded(TreeNode node, bool expanded)
    {
        // TreeViewItem containers are realized lazily; walk what exists.
        var container = Tree.ItemContainerGenerator.ContainerFromItem(node) as TreeViewItem;
        if (container is not null)
        {
            container.IsExpanded = expanded;
        }
    }

    private void UpdateStatus()
    {
        var count = Enumerate(_root).Count();
        TreeStatus.Text = $"节点总数 {count}";
    }
}
